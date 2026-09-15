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
from app.ai.schemas import Intent, InterpretedRequest
from app.domain import appointments as domain_appointments
from app.domain import availability, booking
from app.domain.customers import find_or_create_customer
from app.domain.exceptions import (
    AppointmentNotModifiableError,
    DomainError,
    EntityNotFoundError,
    SlotUnavailableError,
)
from app.domain.models import (
    Appointment,
    AppointmentStatus,
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
    ProcessingRunKind,
    ProcessingRunState,
    ScheduledJob,
    ScheduledJobStatus,
    ScheduledJobType,
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


async def _find_upcoming_appointments(
    db: AsyncSession, business_id: UUID, customer_id: UUID, now: datetime
) -> list[Appointment]:
    """The candidate set for the Phase 7 "which appointment" resolution:
    every still-BOOKED, still-future appointment this customer has. The
    normal flow only ever acts automatically when there's exactly one --
    zero or several are handled by the caller as clean escalations, per
    the approved design. A customer having more than one is a real,
    intentionally-supported case at the data-model level; this is just
    the automated flow declining to guess which one they mean."""
    stmt = (
        select(Appointment)
        .where(
            Appointment.business_id == business_id,
            Appointment.customer_id == customer_id,
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.start_at > now,
        )
        .order_by(Appointment.start_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _handle_cancel_intent(
    db: AsyncSession,
    run: ProcessingRun,
    business: Business,
    now: datetime,
    *,
    notification_service: NotificationService,
    calendar_provider: CalendarProvider,
) -> None:
    candidates = await _find_upcoming_appointments(db, run.business_id, run.customer_id, now)
    if not candidates:
        await _escalate(db, run, "no upcoming appointment found to cancel")
        return
    if len(candidates) > 1:
        await _escalate(
            db, run, "customer has multiple upcoming appointments -- cancellation needs review"
        )
        return

    appointment = candidates[0]
    try:
        appointment = await domain_appointments.cancel_appointment(db, appointment.id, now=now)
    except AppointmentNotModifiableError as exc:
        await _escalate(db, run, str(exc))
        return

    run.resulting_appointment_id = appointment.id
    db.add(
        AuditEvent(
            processing_run_id=run.id,
            entity_type="Appointment",
            entity_id=appointment.id,
            from_state=AppointmentStatus.BOOKED.value,
            to_state=appointment.status.value,
            reason="cancelled via workflow",
        )
    )
    await _transition(db, run, ProcessingRunState.SUCCEEDED)
    await _log_step(db, run, "cancellation_succeeded", {"appointment_id": str(appointment.id)})

    customer = await db.get(Customer, run.customer_id)
    await _sync_calendar_cancel(db, appointment, calendar_provider=calendar_provider, run=run)
    await notification_service.send_cancellation(
        db, appointment=appointment, customer=customer, run=run
    )
    await _log_step(db, run, "cancellation_notice_sent")
    await db.commit()


async def _handle_reschedule_intent(
    db: AsyncSession,
    run: ProcessingRun,
    business: Business,
    req: InterpretedRequest,
    now: datetime,
) -> None:
    candidates = await _find_upcoming_appointments(db, run.business_id, run.customer_id, now)
    if not candidates:
        await _escalate(db, run, "no upcoming appointment found to reschedule")
        return
    if len(candidates) > 1:
        await _escalate(
            db, run, "customer has multiple upcoming appointments -- reschedule needs review"
        )
        return

    appointment = candidates[0]
    if not domain_appointments.can_modify(appointment, business, now):
        await _escalate(
            db,
            run,
            f"appointment {appointment.id} is inside the "
            f"{business.min_reschedule_notice_hours}h reschedule notice window",
        )
        return

    service = await db.get(Service, appointment.service_id)
    run.matched_service_id = service.id
    run.target_appointment_id = appointment.id
    run.resolved_date = req.resolved_date

    # Reuses the exact same slot-offering / AWAITING_CONFIRMATION
    # machinery a fresh booking uses -- confirm_slot below distinguishes
    # "confirm a reschedule" from "confirm a new booking" purely by
    # whether target_appointment_id is set.
    await _offer_slots_or_escalate(db, run, business, service, now, resolved_date=req.resolved_date)


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
    *,
    notification_service: NotificationService,
    calendar_provider: CalendarProvider,
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

    # Ambiguity that still plausibly means an intent this run can act on
    # gets a bounded clarifying question. Anything else ambiguous (a
    # confident unacceptable intent, or no coherent candidates at all)
    # falls through to the same honest escalation as a confident
    # unacceptable intent.
    #
    # For an ordinary customer-initiated run, only BOOK drives this
    # clarification loop -- CANCEL/RESCHEDULE get their own deterministic
    # policy handling below, but only once the AI is confident about the
    # intent. An ambiguous "something about my appointment" doesn't open
    # a second clarification mechanism (the approved Phase 7 design is
    # explicit that there's no appointment-selection conversation for the
    # MVP); it falls through to the same immediate escalation as before.
    # For a no-show RECOVERY run, RESCHEDULE is accepted instead: a
    # customer replying "can I come Thursday instead?" to a recovery
    # message is describing the exact same action as booking a new
    # appointment for the missed one, whichever word the model reaches
    # for.
    acceptable_intents = (
        {Intent.BOOK, Intent.RESCHEDULE}
        if run.kind == ProcessingRunKind.RECOVERY
        else {Intent.BOOK}
    )
    intent_is_acceptable = req.intent in acceptable_intents or bool(
        req.candidate_intents and acceptable_intents & set(req.candidate_intents)
    )

    if req.is_ambiguous and intent_is_acceptable:
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

    # Deterministic CANCEL/RESCHEDULE handling for an ordinary run's
    # *confident* intent only -- an ambiguous cancel/reschedule message
    # already fell through the clarification gate above (it's not in
    # acceptable_intents) and lands on the plain escalation below instead,
    # same as any other still-ambiguous case. A RECOVERY run's RESCHEDULE
    # means "rebook the missed appointment" and is never routed here (it
    # falls through to the ordinary booking logic further down, where
    # matched_service_id is already pre-filled by start_recovery).
    if run.kind == ProcessingRunKind.CUSTOMER_INITIATED and not req.is_ambiguous:
        if req.intent == Intent.CANCEL:
            await _handle_cancel_intent(
                db,
                run,
                business,
                now,
                notification_service=notification_service,
                calendar_provider=calendar_provider,
            )
            return
        if req.intent == Intent.RESCHEDULE:
            await _handle_reschedule_intent(db, run, business, req, now)
            return

    if req.intent not in acceptable_intents:
        await _escalate(
            db,
            run,
            f"intent '{req.intent.value}' is not automated for this conversation",
        )
        return

    if run.matched_service_id is not None:
        # A recovery run already knows the service -- it's the one from
        # the appointment being recovered (set in start_recovery). No
        # need to re-match a hint the customer's reply may not even
        # repeat ("Thursday instead" doesn't name the service again).
        service = await db.get(Service, run.matched_service_id)
    else:
        matched_service_name = match_known_service(req.service_hint, known_services)
        if matched_service_name is None:
            await _escalate(
                db,
                run,
                f"could not match service hint {req.service_hint!r} to a configured service",
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
    notification_service: NotificationService,
    calendar_provider: CalendarProvider,
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
    await _interpret_and_route(
        db,
        run,
        business,
        known_services,
        payload.message,
        interpreter,
        now,
        notification_service=notification_service,
        calendar_provider=calendar_provider,
    )
    await db.commit()
    return run


async def reply_to_clarification(
    db: AsyncSession,
    processing_run_id: UUID,
    message: str,
    *,
    interpreter: Interpreter,
    notification_service: NotificationService,
    calendar_provider: CalendarProvider,
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
    await _interpret_and_route(
        db,
        run,
        business,
        known_services,
        message,
        interpreter,
        now,
        notification_service=notification_service,
        calendar_provider=calendar_provider,
    )
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

    if run.target_appointment_id is not None:
        return await _confirm_reschedule(
            db,
            run,
            business=business,
            service=service,
            new_start_at=payload.start_at,
            notification_service=notification_service,
            calendar_provider=calendar_provider,
            now=now,
        )

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
    if run.kind == ProcessingRunKind.RECOVERY:
        # Link forward, never rewrite: the original stays immutably
        # NO_SHOW; this new row is what actually got booked.
        appointment.rebooked_from_id = run.recovery_of_appointment_id
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

    await _schedule_reminder(db, appointment, business)
    await _log_step(db, run, "reminder_scheduled")

    await db.commit()
    return run


async def _confirm_reschedule(
    db: AsyncSession,
    run: ProcessingRun,
    *,
    business: Business,
    service: Service,
    new_start_at: datetime,
    notification_service: NotificationService,
    calendar_provider: CalendarProvider,
    now: datetime,
) -> ProcessingRun:
    """The reschedule counterpart to the booking branch above: same
    AWAITING_CONFIRMATION -> BOOKING transition and re-offer-on-conflict
    behavior, but moves run.target_appointment_id in place via
    domain.appointments.reschedule_appointment instead of creating a new
    Appointment."""
    await _transition(db, run, ProcessingRunState.BOOKING)
    await _log_step(db, run, "reschedule_attempted")

    try:
        appointment = await domain_appointments.reschedule_appointment(
            db, run.target_appointment_id, new_start_at, now=now
        )
    except SlotUnavailableError:
        await _log_step(db, run, "slot_no_longer_available", {"start_at": new_start_at.isoformat()})
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
            from_state="BOOKED",
            to_state="BOOKED",
            reason="rescheduled via workflow",
        )
    )
    await _transition(db, run, ProcessingRunState.SUCCEEDED)
    await _log_step(db, run, "reschedule_succeeded", {"appointment_id": str(appointment.id)})

    staff = await db.get(StaffResource, appointment.staff_id)
    customer = await db.get(Customer, run.customer_id)

    # Same best-effort, never-blocking discipline as a fresh booking's
    # calendar sync: a failure here is recorded and never undoes the
    # reschedule, which already committed in Postgres.
    await _sync_calendar_update(
        db,
        appointment,
        business=business,
        service=service,
        staff=staff,
        customer=customer,
        calendar_provider=calendar_provider,
        run=run,
    )

    await notification_service.send_reschedule_confirmation(
        db, appointment=appointment, customer=customer, run=run
    )
    await _log_step(db, run, "reschedule_confirmation_sent")

    await _reschedule_pending_reminder(db, appointment, business)

    await db.commit()
    return run


async def _schedule_reminder(
    db: AsyncSession, appointment: Appointment, business: Business
) -> None:
    """One-off, entity-specific deferred work -- goes through
    ScheduledJob, not a periodic sweep (see app.workflow.jobs). The
    reminder job re-checks the appointment is still BOOKED before
    sending, so this is safe to schedule even though the appointment
    could be cancelled long before run_at arrives."""
    run_at = appointment.start_at - timedelta(hours=business.reminder_lead_hours)
    db.add(
        ScheduledJob(
            job_type=ScheduledJobType.SEND_REMINDER,
            run_at=run_at,
            payload={"appointment_id": str(appointment.id)},
        )
    )
    await db.flush()


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


async def _sync_calendar_update(
    db: AsyncSession,
    appointment: Appointment,
    *,
    business: Business,
    service: Service,
    staff: StaffResource,
    customer: Customer,
    calendar_provider: CalendarProvider,
    run: ProcessingRun,
) -> None:
    """Mirrors a reschedule onto the calendar. If the original booking
    never successfully synced (no calendar_event_id yet), there's nothing
    to update -- falls back to creating the event fresh, the same
    recovery a manual retry_calendar_sync would eventually do anyway."""
    if appointment.calendar_event_id is None:
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
        return

    try:
        await calendar_provider.update_event(
            calendar_event_id=appointment.calendar_event_id,
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
                processing_run_id=run.id,
                entity_type="Appointment",
                entity_id=appointment.id,
                from_state="calendar_sync:SYNCED",
                to_state="calendar_sync:FAILED",
                reason=str(exc),
            )
        )
        await _log_step(db, run, "calendar_sync_failed", {"error": str(exc)})
        await db.flush()
        return

    appointment.calendar_sync_status = CalendarSyncStatus.SYNCED
    db.add(
        AuditEvent(
            processing_run_id=run.id,
            entity_type="Appointment",
            entity_id=appointment.id,
            from_state="calendar_sync:SYNCED",
            to_state="calendar_sync:SYNCED",
            reason="reschedule mirrored to calendar",
        )
    )
    await _log_step(
        db, run, "calendar_sync_succeeded", {"calendar_event_id": appointment.calendar_event_id}
    )
    await db.flush()


async def _sync_calendar_cancel(
    db: AsyncSession,
    appointment: Appointment,
    *,
    calendar_provider: CalendarProvider,
    run: ProcessingRun,
) -> None:
    """Best-effort calendar cancellation -- never undoes the Postgres
    cancellation, which has already committed by the time this runs. If
    the appointment never had a synced event, there's nothing to cancel
    on the provider's side."""
    if appointment.calendar_event_id is None:
        return

    try:
        await calendar_provider.cancel_event(calendar_event_id=appointment.calendar_event_id)
    except CalendarProviderError as exc:
        db.add(
            AuditEvent(
                processing_run_id=run.id,
                entity_type="Appointment",
                entity_id=appointment.id,
                from_state="calendar_sync:SYNCED",
                to_state="calendar_sync:FAILED",
                reason=str(exc),
            )
        )
        await _log_step(db, run, "calendar_cancel_failed", {"error": str(exc)})
        await db.flush()
        return

    db.add(
        AuditEvent(
            processing_run_id=run.id,
            entity_type="Appointment",
            entity_id=appointment.id,
            from_state="calendar_sync:SYNCED",
            to_state="calendar_sync:CANCELLED",
            reason="cancellation mirrored to calendar",
        )
    )
    await _log_step(
        db, run, "calendar_cancel_succeeded", {"calendar_event_id": appointment.calendar_event_id}
    )
    await db.flush()


async def _reschedule_pending_reminder(
    db: AsyncSession, appointment: Appointment, business: Business
) -> None:
    """A SEND_REMINDER job scheduled at the appointment's original
    start_at is now scheduled for the wrong time -- move it, rather than
    let it fire (or not) relative to a start_at this appointment no
    longer has. A job that's already DONE, LOCKED, or otherwise not
    PENDING is left alone: nothing to reschedule."""
    job = (
        await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.job_type == ScheduledJobType.SEND_REMINDER,
                ScheduledJob.status == ScheduledJobStatus.PENDING,
                ScheduledJob.payload["appointment_id"].astext == str(appointment.id),
            )
        )
    ).scalar_one_or_none()
    if job is None:
        return
    job.run_at = appointment.start_at - timedelta(hours=business.reminder_lead_hours)
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


async def start_recovery(
    db: AsyncSession,
    appointment_id: UUID,
    *,
    notification_service: NotificationService,
    now: datetime | None = None,
) -> ProcessingRun:
    """Opens a no-show recovery attempt: sends the outreach message and
    creates a kind=RECOVERY ProcessingRun sitting in
    AWAITING_CLARIFICATION, ready for the customer's reply through the
    exact same reply_to_clarification / POST /reply path a clarifying
    question uses -- no parallel endpoint, no parallel state. Called by
    the no-show sweep (app.workflow.jobs) immediately after an
    appointment is marked NO_SHOW.
    """
    now = now or datetime.now(UTC)

    appointment = await db.get(Appointment, appointment_id)
    if appointment is None:
        raise EntityNotFoundError("Appointment", appointment_id)
    if appointment.status != AppointmentStatus.NO_SHOW:
        raise DomainError(
            f"Appointment {appointment_id} is {appointment.status.value}, not NO_SHOW"
        )

    customer = await db.get(Customer, appointment.customer_id)

    run = ProcessingRun(
        business_id=appointment.business_id,
        customer_id=appointment.customer_id,
        raw_message="(system-initiated no-show recovery outreach)",
        state=ProcessingRunState.RECEIVED,
        kind=ProcessingRunKind.RECOVERY,
        recovery_of_appointment_id=appointment.id,
        # Already known from the missed appointment -- a reply like
        # "Thursday instead?" won't repeat the service name, so there is
        # nothing to re-match against known_services later.
        matched_service_id=appointment.service_id,
        messages=[],
    )
    db.add(run)
    await db.flush()
    await _log_step(db, run, "no_show_recovery_opened", {"appointment_id": str(appointment.id)})

    notification = await notification_service.send_no_show_recovery(
        db, appointment=appointment, customer=customer, run=run
    )
    _append_message(run, "assistant", notification.body, now)
    await _log_step(db, run, "recovery_outreach_sent")

    await _transition(db, run, ProcessingRunState.AWAITING_CLARIFICATION)
    await db.commit()
    return run
