"""The workflow orchestrator: the one place that composes app.domain and
app.ai into the actual request -> interpretation -> availability -> offer
-> confirm -> book lifecycle. Domain and ai stay independent of each other
and of this module; this module depends on both, plus the Interpreter and
NotificationService Protocols -- never a vendor SDK directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.exceptions import AIInterpretationError
from app.ai.interpreter import Interpreter
from app.ai.resolve import match_known_service
from app.ai.schemas import Intent
from app.domain import availability, booking
from app.domain.exceptions import DomainError, SlotUnavailableError
from app.domain.models import Business, Customer, Service, StaffResource, staff_services
from app.domain.schemas import BookingRequest
from app.workflow.exceptions import (
    InvalidSlotChoiceError,
    ProcessingRunNotFoundError,
    ProcessingRunStateError,
)
from app.workflow.models import (
    AIInvocation,
    AuditEvent,
    EscalationCase,
    ProcessingRun,
    ProcessingRunState,
    WorkflowStep,
)
from app.workflow.notifications import NotificationService
from app.workflow.schemas import ConfirmSlotIn, StartRequestIn
from app.workflow.state_machine import validate_transition

MAX_OFFERED_SLOTS = 3
DEFAULT_SEARCH_DAYS = 7


@dataclass(frozen=True)
class _Offer:
    staff_id: UUID
    start_at: datetime
    end_at: datetime


async def _transition(
    db: AsyncSession, run: ProcessingRun, target: ProcessingRunState, *, reason: str | None = None
) -> None:
    validate_transition(run.state, target)
    db.add(
        AuditEvent(
            processing_run_id=run.id,
            entity_type="ProcessingRun",
            entity_id=run.id,
            from_state=run.state.value,
            to_state=target.value,
            reason=reason,
        )
    )
    run.state = target
    await db.flush()


async def _log_step(
    db: AsyncSession, run: ProcessingRun, step_name: str, detail: dict | None = None
) -> None:
    db.add(WorkflowStep(processing_run_id=run.id, step_name=step_name, detail=detail))
    await db.flush()


async def _escalate(db: AsyncSession, run: ProcessingRun, reason: str) -> None:
    await _transition(db, run, ProcessingRunState.ESCALATED, reason=reason)
    await _log_step(db, run, "escalated", {"reason": reason})
    db.add(EscalationCase(processing_run_id=run.id, reason=reason))
    await db.commit()


def _day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return start_local.astimezone(UTC), (start_local + timedelta(days=1)).astimezone(UTC)


def _default_window(tz: ZoneInfo, now: datetime) -> tuple[datetime, datetime]:
    tomorrow_local = (now.astimezone(tz) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = tomorrow_local.astimezone(UTC)
    end = (tomorrow_local + timedelta(days=DEFAULT_SEARCH_DAYS)).astimezone(UTC)
    return start, end


async def _eligible_staff(
    db: AsyncSession, business_id: UUID, service_id: UUID
) -> list[StaffResource]:
    stmt = (
        select(StaffResource)
        .join(staff_services, staff_services.c.staff_id == StaffResource.id)
        .where(
            staff_services.c.service_id == service_id,
            StaffResource.business_id == business_id,
            StaffResource.active.is_(True),
        )
    )
    return list((await db.execute(stmt)).scalars().all())


async def _gather_offers(
    db: AsyncSession,
    *,
    business: Business,
    service: Service,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> list[_Offer]:
    staff_members = await _eligible_staff(db, business.id, service.id)
    offers: list[_Offer] = []
    for staff in staff_members:
        slots = await availability.list_available_slots(
            db,
            business=business,
            service=service,
            staff=staff,
            window_start=window_start,
            window_end=window_end,
            now=now,
        )
        offers.extend(_Offer(staff.id, s.start_at, s.end_at) for s in slots)
    offers.sort(key=lambda o: o.start_at)
    return offers[:MAX_OFFERED_SLOTS]


def _offers_to_json(offers: list[_Offer]) -> list[dict]:
    return [
        {
            "staff_id": str(o.staff_id),
            "start_at": o.start_at.isoformat(),
            "end_at": o.end_at.isoformat(),
        }
        for o in offers
    ]


async def _offer_slots_or_escalate(
    db: AsyncSession,
    run: ProcessingRun,
    business: Business,
    service: Service,
    now: datetime,
    *,
    resolved_date: date | None,
) -> bool:
    """Computes offers for the given date context and moves the run to
    AWAITING_CONFIRMATION, or escalates if nothing is available. Returns
    True if slots were offered."""
    tz = ZoneInfo(business.timezone)
    if resolved_date is not None:
        window_start, window_end = _day_window(resolved_date, tz)
    else:
        window_start, window_end = _default_window(tz, now)

    offers = await _gather_offers(
        db,
        business=business,
        service=service,
        window_start=window_start,
        window_end=window_end,
        now=now,
    )
    if not offers:
        await _escalate(db, run, "no availability found in the requested window")
        return False

    run.offered_slots = _offers_to_json(offers)
    await _transition(db, run, ProcessingRunState.SLOTS_OFFERED)
    await _log_step(db, run, "slots_offered", {"count": len(offers)})
    await _transition(db, run, ProcessingRunState.AWAITING_CONFIRMATION)
    await _log_step(db, run, "awaiting_confirmation")
    return True


async def start_request(
    db: AsyncSession,
    payload: StartRequestIn,
    *,
    interpreter: Interpreter,
    now: datetime | None = None,
) -> ProcessingRun:
    now = now or datetime.now(UTC)

    business = await db.get(Business, payload.business_id)
    if business is None:
        raise DomainError(f"Business {payload.business_id} not found")

    run = ProcessingRun(
        business_id=payload.business_id,
        customer_id=payload.customer_id,
        raw_message=payload.message,
        state=ProcessingRunState.RECEIVED,
    )
    db.add(run)
    await db.flush()
    await _log_step(db, run, "received")
    await _transition(db, run, ProcessingRunState.INTERPRETING)

    known_services = list(
        (
            await db.execute(
                select(Service.name).where(
                    Service.business_id == business.id, Service.active.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )

    try:
        outcome = await interpreter.interpret(
            payload.message, today=now.date(), known_services=known_services
        )
    except AIInterpretationError as exc:
        await _transition(db, run, ProcessingRunState.FAILED, reason=str(exc))
        await _log_step(db, run, "interpretation_failed", {"error": str(exc)})
        await db.commit()
        return run

    req = outcome.request
    db.add(
        AIInvocation(
            processing_run_id=run.id,
            provider=outcome.metadata.provider,
            model=outcome.metadata.model,
            latency_ms=outcome.metadata.latency_ms,
            input_tokens=outcome.metadata.input_tokens,
            output_tokens=outcome.metadata.output_tokens,
            intent=req.intent.value,
            service_hint=req.service_hint,
            date_hint=req.date_hint,
            resolved_date=req.resolved_date,
            time_preference=req.time_preference,
            is_ambiguous=req.is_ambiguous,
            ambiguity_reason=req.ambiguity_reason,
            confidence=req.confidence,
        )
    )
    await _log_step(
        db, run, "interpreted", {"intent": req.intent.value, "confidence": req.confidence}
    )

    # Phase 3 automates the booking path only -- cancel/reschedule get
    # their own deterministic policy handling in Phase 6. Anything else
    # is a clean, honest escalation rather than a half-implemented guess.
    if req.intent != Intent.BOOK:
        await _escalate(
            db,
            run,
            f"intent '{req.intent.value}' is not yet automated (booking only in this phase)",
        )
        return run

    if req.is_ambiguous:
        await _escalate(
            db, run, req.ambiguity_reason or "request flagged ambiguous by interpretation"
        )
        return run

    matched_service_name = match_known_service(req.service_hint, known_services)
    if matched_service_name is None:
        await _escalate(
            db, run, f"could not match service hint {req.service_hint!r} to a configured service"
        )
        return run

    service = (
        await db.execute(
            select(Service).where(
                Service.business_id == business.id, Service.name == matched_service_name
            )
        )
    ).scalar_one()
    run.matched_service_id = service.id
    run.resolved_date = req.resolved_date

    await _offer_slots_or_escalate(db, run, business, service, now, resolved_date=req.resolved_date)
    await db.commit()
    return run


async def confirm_slot(
    db: AsyncSession,
    processing_run_id: UUID,
    payload: ConfirmSlotIn,
    *,
    notification_service: NotificationService,
    now: datetime | None = None,
) -> ProcessingRun:
    now = now or datetime.now(UTC)

    run = await db.get(ProcessingRun, processing_run_id)
    if run is None:
        raise ProcessingRunNotFoundError(processing_run_id)
    if run.state != ProcessingRunState.AWAITING_CONFIRMATION:
        raise ProcessingRunStateError(
            f"ProcessingRun {processing_run_id} is {run.state.value}, not AWAITING_CONFIRMATION"
        )

    chosen = next(
        (
            o
            for o in (run.offered_slots or [])
            if datetime.fromisoformat(o["start_at"]) == payload.start_at
        ),
        None,
    )
    if chosen is None:
        raise InvalidSlotChoiceError("chosen start_at does not match any offered slot")

    business = await db.get(Business, run.business_id)
    service = await db.get(Service, run.matched_service_id)

    await _transition(db, run, ProcessingRunState.BOOKING)
    await _log_step(db, run, "booking_attempted")

    booking_request = BookingRequest(
        idempotency_key=payload.idempotency_key,
        business_id=run.business_id,
        service_id=run.matched_service_id,
        staff_id=UUID(chosen["staff_id"]),
        customer_id=run.customer_id,
        start_at=payload.start_at,
    )
    try:
        appointment = await booking.book_appointment(db, booking_request, now=now)
    except SlotUnavailableError:
        # Exactly the exception path the architecture baseline calls out
        # (Section C): re-check for real, re-offer, never just error out.
        await _log_step(
            db, run, "slot_no_longer_available", {"start_at": payload.start_at.isoformat()}
        )
        await _offer_slots_or_escalate(
            db, run, business, service, now, resolved_date=run.resolved_date
        )
        await db.commit()
        return run
    except DomainError as exc:
        await _escalate(db, run, str(exc))
        return run

    run.resulting_appointment_id = appointment.id
    db.add(
        AuditEvent(
            processing_run_id=run.id,
            entity_type="Appointment",
            entity_id=appointment.id,
            from_state=None,
            to_state=appointment.status.value,
            reason="booked via workflow",
        )
    )
    await _transition(db, run, ProcessingRunState.SUCCEEDED)
    await _log_step(db, run, "booking_succeeded", {"appointment_id": str(appointment.id)})

    customer = await db.get(Customer, run.customer_id)
    await notification_service.send_confirmation(
        db, appointment=appointment, customer=customer, run=run
    )
    await _log_step(db, run, "confirmation_sent")

    await db.commit()
    return run
