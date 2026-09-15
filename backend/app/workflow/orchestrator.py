"""The workflow orchestrator: the one place that composes app.domain and
app.ai into the actual request -> interpretation -> [clarification] ->
availability -> offer -> confirm -> book lifecycle. Domain and ai stay
independent of each other and of this module; this module depends on
both, plus the Interpreter and NotificationService Protocols -- never a
vendor SDK directly.
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
from app.domain.customers import find_or_create_customer
from app.domain.exceptions import DomainError, EntityNotFoundError, SlotUnavailableError
from app.domain.models import (
    Appointment,
    Business,
    CalendarSyncStatus,
    Customer,
    Service,
    StaffResource,
    staff_services,
)
from app.domain.schemas import BookingRequest
from app.workflow.calendar import CalendarProvider, CalendarProviderError
from app.workflow.clarify import derive_clarifying_question
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
# Bounded, not an open-ended chatbot: at most this many clarifying
# questions before we stop guessing and hand off to a human. Matches the
# architecture proposal's original exception-path language for ambiguous
# requests, not a new number invented for chat.
MAX_CLARIFICATION_ROUNDS = 2


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


def _append_message(run: ProcessingRun, role: str, content: str, now: datetime) -> None:
    entry = {"role": role, "content": content, "at": now.isoformat()}
    run.messages = [*(run.messages or []), entry]


def _render_transcript(messages: list[dict]) -> str:
    """Flattens the bounded conversation into one string for the existing
    single-message Interpreter Protocol (app.ai.interpreter, Phase 2 --
    unchanged). Keeping the Protocol's signature untouched is deliberate:
    multi-turn framing is this orchestrator's concern, not the AI layer's."""
    lines = []
    for m in messages:
        label = "Customer" if m["role"] == "customer" else "Assistant (clarifying)"
        lines.append(f"{label}: {m['content']}")
    return "\n".join(lines)


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


async def _known_services(db: AsyncSession, business_id: UUID) -> list[str]:
    return list(
        (
            await db.execute(
                select(Service.name).where(
                    Service.business_id == business_id, Service.active.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )


async def _interpret_and_route(
    db: AsyncSession,
    run: ProcessingRun,
    business: Business,
    known_services: list[str],
    message: str,
    interpreter: Interpreter,
    now: datetime,
) -> None:
    """Shared by start_request (first message) and reply_to_clarification
    (a follow-up): append the message, interpret the full transcript so
    far, then route to slots-offered / a bounded clarifying question /
    escalation. The one place this decision is made, regardless of which
    turn of the conversation triggered it.
    """
    _append_message(run, "customer", message, now)
    transcript = _render_transcript(run.messages)

    try:
        outcome = await interpreter.interpret(
            transcript, today=now.date(), known_services=known_services
        )
    except AIInterpretationError as exc:
        await _transition(db, run, ProcessingRunState.FAILED, reason=str(exc))
        await _log_step(db, run, "interpretation_failed", {"error": str(exc)})
        return

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
            candidate_intents=(
                [i.value for i in req.candidate_intents] if req.candidate_intents else None
            ),
            confidence=req.confidence,
        )
    )
    await _log_step(
        db, run, "interpreted", {"intent": req.intent.value, "confidence": req.confidence}
    )

    # Ambiguity that still plausibly means BOOK -- either the model
    # committed to intent=book with missing detail, or it flagged BOOK as
    # one of a small set of candidate intents (architecture review,
    # Finding 1) -- gets a bounded clarifying question. Anything else
    # ambiguous (confident non-BOOK, or no coherent candidates at all)
    # falls through to the same honest escalation as a confident non-BOOK
    # intent: Phase 3 automates the booking path only, and clarification
    # stays scoped to it.
    book_is_candidate = req.intent == Intent.BOOK or (
        req.candidate_intents is not None and Intent.BOOK in req.candidate_intents
    )

    if req.is_ambiguous and book_is_candidate:
        if run.clarification_rounds >= MAX_CLARIFICATION_ROUNDS:
            await _escalate(
                db,
                run,
                f"still ambiguous after {MAX_CLARIFICATION_ROUNDS} clarification rounds: "
                f"{req.ambiguity_reason}",
            )
            return

        question = derive_clarifying_question(req)
        run.clarification_rounds += 1
        _append_message(run, "assistant", question, now)
        await _transition(db, run, ProcessingRunState.AWAITING_CLARIFICATION)
        await _log_step(
            db,
            run,
            "clarification_requested",
            {
                "question": question,
                "round": run.clarification_rounds,
                "reason": req.ambiguity_reason,
            },
        )
        return

    if req.intent != Intent.BOOK:
        await _escalate(
            db,
            run,
            f"intent '{req.intent.value}' is not yet automated (booking only in this phase)",
        )
        return

    matched_service_name = match_known_service(req.service_hint, known_services)
    if matched_service_name is None:
        await _escalate(
            db, run, f"could not match service hint {req.service_hint!r} to a configured service"
        )
        return

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

    if payload.customer_id is not None:
        customer_id = payload.customer_id
    else:
        customer = await find_or_create_customer(
            db,
            business_id=business.id,
            name=payload.customer_name,  # type: ignore[arg-type]
            contact=payload.customer_contact,  # type: ignore[arg-type]
        )
        customer_id = customer.id

    run = ProcessingRun(
        business_id=payload.business_id,
        customer_id=customer_id,
        raw_message=payload.message,
        state=ProcessingRunState.RECEIVED,
        messages=[],
    )
    db.add(run)
    await db.flush()
    await _log_step(db, run, "received")
    await _transition(db, run, ProcessingRunState.INTERPRETING)

    known_services = await _known_services(db, business.id)
    await _interpret_and_route(db, run, business, known_services, payload.message, interpreter, now)
    await db.commit()
    return run


async def reply_to_clarification(
    db: AsyncSession,
    processing_run_id: UUID,
    message: str,
    *,
    interpreter: Interpreter,
    now: datetime | None = None,
) -> ProcessingRun:
    now = now or datetime.now(UTC)

    run = await db.get(ProcessingRun, processing_run_id)
    if run is None:
        raise ProcessingRunNotFoundError(processing_run_id)
    if run.state != ProcessingRunState.AWAITING_CLARIFICATION:
        raise ProcessingRunStateError(
            f"ProcessingRun {processing_run_id} is {run.state.value}, not AWAITING_CLARIFICATION"
        )

    business = await db.get(Business, run.business_id)
    await _transition(db, run, ProcessingRunState.INTERPRETING)
    await _log_step(db, run, "clarification_reply_received")

    known_services = await _known_services(db, business.id)
    await _interpret_and_route(db, run, business, known_services, message, interpreter, now)
    await db.commit()
    return run


async def confirm_slot(
    db: AsyncSession,
    processing_run_id: UUID,
    payload: ConfirmSlotIn,
    *,
    notification_service: NotificationService,
    calendar_provider: CalendarProvider,
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

    staff = await db.get(StaffResource, appointment.staff_id)
    customer = await db.get(Customer, run.customer_id)

    # Best-effort, never blocking: a calendar sync failure must not
    # invalidate a booking that already committed, and must not delay or
    # skip the customer's confirmation below (architecture baseline,
    # Section H/K).
    await _sync_calendar(
        db,
        appointment,
        business=business,
        service=service,
        staff=staff,
        customer=customer,
        calendar_provider=calendar_provider,
        run=run,
    )

    await notification_service.send_confirmation(
        db, appointment=appointment, customer=customer, run=run
    )
    await _log_step(db, run, "confirmation_sent")

    await db.commit()
    return run


async def _sync_calendar(
    db: AsyncSession,
    appointment: Appointment,
    *,
    business: Business,
    service: Service,
    staff: StaffResource,
    customer: Customer,
    calendar_provider: CalendarProvider,
    run: ProcessingRun | None = None,
) -> None:
    """Attempts to mirror a BOOKED appointment onto the external calendar.
    Success or failure, the appointment's own status is never touched
    here -- only calendar_event_id / calendar_sync_status. `run` is
    optional so this same function serves both the confirm-time sync
    (inside a ProcessingRun) and the standalone manual retry endpoint
    (no ProcessingRun context)."""
    try:
        event_id = await calendar_provider.create_event(
            appointment=appointment,
            business=business,
            service=service,
            staff=staff,
            customer=customer,
        )
    except CalendarProviderError as exc:
        appointment.calendar_sync_status = CalendarSyncStatus.FAILED
        db.add(
            AuditEvent(
                processing_run_id=run.id if run else None,
                entity_type="Appointment",
                entity_id=appointment.id,
                from_state=CalendarSyncStatus.PENDING.value,
                to_state="calendar_sync:FAILED",
                reason=str(exc),
            )
        )
        if run is not None:
            await _log_step(db, run, "calendar_sync_failed", {"error": str(exc)})
        await db.flush()
        return

    appointment.calendar_event_id = event_id
    appointment.calendar_sync_status = CalendarSyncStatus.SYNCED
    db.add(
        AuditEvent(
            processing_run_id=run.id if run else None,
            entity_type="Appointment",
            entity_id=appointment.id,
            from_state=CalendarSyncStatus.PENDING.value,
            to_state="calendar_sync:SYNCED",
            reason=None,
        )
    )
    if run is not None:
        await _log_step(db, run, "calendar_sync_succeeded", {"calendar_event_id": event_id})
    await db.flush()


async def retry_calendar_sync(
    db: AsyncSession, appointment_id: UUID, *, calendar_provider: CalendarProvider
) -> Appointment:
    """Manual, idempotent retry -- not the automated sweep (that's a
    later phase's background-job infrastructure). Safe to call on any
    appointment at any time: already-SYNCED is a harmless no-op, and a
    repeated failure just records another attempt without corrupting
    anything."""
    appointment = await db.get(Appointment, appointment_id)
    if appointment is None:
        raise EntityNotFoundError("Appointment", appointment_id)

    if appointment.calendar_sync_status == CalendarSyncStatus.SYNCED:
        return appointment

    business = await db.get(Business, appointment.business_id)
    service = await db.get(Service, appointment.service_id)
    staff = await db.get(StaffResource, appointment.staff_id)
    customer = await db.get(Customer, appointment.customer_id)

    await _sync_calendar(
        db,
        appointment,
        business=business,
        service=service,
        staff=staff,
        customer=customer,
        calendar_provider=calendar_provider,
        run=None,
    )
    await db.commit()
    await db.refresh(appointment)
    return appointment
