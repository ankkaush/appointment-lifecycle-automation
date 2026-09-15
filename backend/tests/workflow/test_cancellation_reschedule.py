"""Phase 7: cancellation and rescheduling. AI interprets CANCEL/RESCHEDULE
intent; deterministic code resolves which appointment (exactly one
upcoming BOOKED appointment, or a clean escalation), checks the shared
notice-window policy, and either cancels immediately or reuses the
existing slot-offering/confirmation machinery to move the same
appointment in place. No live API calls -- FakeInterpreter and Mock
providers throughout.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.fake import FakeInterpreter
from app.domain import booking as domain_booking
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Business,
    CalendarSyncStatus,
    Customer,
    Service,
    StaffResource,
)
from app.domain.schemas import BookingRequest
from app.workflow import orchestrator
from app.workflow.calendar import CalendarProviderError, MockCalendarProvider
from app.workflow.models import (
    AuditEvent,
    EscalationCase,
    Notification,
    ProcessingRunState,
    ScheduledJob,
    ScheduledJobType,
    WorkflowStep,
)
from app.workflow.notifications import MockNotificationProvider
from app.workflow.schemas import ConfirmSlotIn, StartRequestIn


@dataclass
class _FailingCalendarProvider:
    message: str = "simulated calendar outage"

    async def create_event(self, **kwargs) -> str:
        raise CalendarProviderError(self.message)

    async def update_event(self, **kwargs) -> None:
        raise CalendarProviderError(self.message)

    async def cancel_event(self, **kwargs) -> None:
        raise CalendarProviderError(self.message)


async def _book(
    db: AsyncSession, business: Business, customer: Customer, *, now: datetime | None = None
) -> Appointment:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Book me a Haircut please"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )
    chosen = run.offered_slots[0]
    confirm_payload = ConfirmSlotIn(
        start_at=datetime.fromisoformat(chosen["start_at"]), idempotency_key=str(uuid4())
    )
    result = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )
    return await db.get(Appointment, result.resulting_appointment_id)


# --- cancellation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_intent_cancels_the_single_upcoming_appointment(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Please cancel my appointment"
    )
    now = appointment.start_at - timedelta(hours=48)
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    assert run.state == ProcessingRunState.SUCCEEDED
    assert run.resulting_appointment_id == appointment.id

    await db.refresh(appointment)
    assert appointment.status == AppointmentStatus.CANCELLED

    notification = (
        await db.execute(
            select(Notification).where(
                Notification.appointment_id == appointment.id,
                Notification.subject == "Appointment cancelled",
            )
        )
    ).scalar_one()
    assert notification.to_contact == customer.contact

    calendar_event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.entity_type == "Appointment",
                AuditEvent.entity_id == appointment.id,
                AuditEvent.to_state == "calendar_sync:CANCELLED",
            )
        )
    ).scalar_one_or_none()
    assert calendar_event is not None


@pytest.mark.asyncio
async def test_cancel_intent_calendar_failure_does_not_block_cancellation(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="cancel my appointment please"
    )
    now = appointment.start_at - timedelta(hours=48)
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=_FailingCalendarProvider(),
        now=now,
    )

    # The Postgres cancellation already committed inside
    # domain.appointments.cancel_appointment before the calendar call
    # ever ran -- a provider outage can't undo it.
    assert run.state == ProcessingRunState.SUCCEEDED
    await db.refresh(appointment)
    assert appointment.status == AppointmentStatus.CANCELLED

    notification = (
        await db.execute(
            select(Notification).where(
                Notification.appointment_id == appointment.id,
                Notification.subject == "Appointment cancelled",
            )
        )
    ).scalar_one()
    assert notification is not None


@pytest.mark.asyncio
async def test_cancel_intent_escalates_when_no_upcoming_appointment(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="cancel my appointment please"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.ESCALATED
    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert "no upcoming appointment" in case.reason


@pytest.mark.asyncio
async def test_cancel_intent_escalates_when_multiple_upcoming_appointments(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    first = await _book(db, business, customer)
    second = await _book(db, business, customer)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="cancel my appointment please"
    )
    now = min(first.start_at, second.start_at) - timedelta(hours=48)
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    assert run.state == ProcessingRunState.ESCALATED
    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert "multiple upcoming appointments" in case.reason

    await db.refresh(first)
    await db.refresh(second)
    assert first.status == AppointmentStatus.BOOKED
    assert second.status == AppointmentStatus.BOOKED


@pytest.mark.asyncio
async def test_cancel_intent_rejected_inside_notice_window(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="cancel my appointment please"
    )
    # Business.min_reschedule_notice_hours defaults to 24 -- one hour out
    # is well inside that window.
    now = appointment.start_at - timedelta(hours=1)
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    assert run.state == ProcessingRunState.ESCALATED
    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert "notice window" in case.reason

    await db.refresh(appointment)
    assert appointment.status == AppointmentStatus.BOOKED


# --- rescheduling --------------------------------------------------------


@pytest.mark.asyncio
async def test_reschedule_intent_offers_slots_then_confirm_moves_same_appointment(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    original_id = appointment.id
    original_start = appointment.start_at

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="I need to reschedule please"
    )
    now = original_start - timedelta(hours=48)
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    assert run.state == ProcessingRunState.AWAITING_CONFIRMATION
    assert run.offered_slots
    assert run.target_appointment_id == original_id

    chosen = run.offered_slots[0]
    new_start_at = datetime.fromisoformat(chosen["start_at"])
    confirm_payload = ConfirmSlotIn(start_at=new_start_at, idempotency_key=str(uuid4()))
    result = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    assert result.state == ProcessingRunState.SUCCEEDED
    assert result.resulting_appointment_id == original_id  # same row, same id

    await db.refresh(appointment)
    assert appointment.id == original_id
    assert appointment.start_at == new_start_at
    assert appointment.start_at != original_start
    assert appointment.status == AppointmentStatus.BOOKED
    assert appointment.calendar_sync_status == CalendarSyncStatus.SYNCED

    notification = (
        await db.execute(
            select(Notification).where(
                Notification.appointment_id == original_id,
                Notification.subject == "Appointment rescheduled",
            )
        )
    ).scalar_one()
    assert notification.to_contact == customer.contact


@pytest.mark.asyncio
async def test_reschedule_updates_pending_reminder_job_to_new_time(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    now = appointment.start_at - timedelta(hours=48)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="I need to reschedule please"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )
    new_start_at = datetime.fromisoformat(run.offered_slots[0]["start_at"])
    confirm_payload = ConfirmSlotIn(start_at=new_start_at, idempotency_key=str(uuid4()))
    await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    job = (
        await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.job_type == ScheduledJobType.SEND_REMINDER,
                ScheduledJob.payload["appointment_id"].astext == str(appointment.id),
            )
        )
    ).scalar_one()
    assert job.run_at == new_start_at - timedelta(hours=business.reminder_lead_hours)


@pytest.mark.asyncio
async def test_reschedule_calendar_failure_does_not_block_the_move(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    now = appointment.start_at - timedelta(hours=48)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="I need to reschedule please"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )
    new_start_at = datetime.fromisoformat(run.offered_slots[0]["start_at"])
    confirm_payload = ConfirmSlotIn(start_at=new_start_at, idempotency_key=str(uuid4()))
    result = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=_FailingCalendarProvider(),
        now=now,
    )

    assert result.state == ProcessingRunState.SUCCEEDED
    await db.refresh(appointment)
    assert appointment.start_at == new_start_at
    assert appointment.status == AppointmentStatus.BOOKED
    assert appointment.calendar_sync_status == CalendarSyncStatus.FAILED

    notification = (
        await db.execute(
            select(Notification).where(
                Notification.appointment_id == appointment.id,
                Notification.subject == "Appointment rescheduled",
            )
        )
    ).scalar_one()
    assert notification is not None


@pytest.mark.asyncio
async def test_reschedule_reoffers_when_chosen_new_slot_taken_concurrently(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    now = appointment.start_at - timedelta(hours=48)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="I need to reschedule please"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )
    original_offer = run.offered_slots[0]
    contested_start = datetime.fromisoformat(original_offer["start_at"])

    # Another customer books that exact slot first, between the offer
    # and the reschedule confirm.
    other_customer = Customer(
        business_id=business.id, name="Other Customer", contact="other-r@example.com"
    )
    db.add(other_customer)
    await db.flush()
    await domain_booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=other_customer.id,
            start_at=contested_start,
        ),
        now=now,
    )

    confirm_payload = ConfirmSlotIn(start_at=contested_start, idempotency_key=str(uuid4()))
    result = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    assert result.state == ProcessingRunState.AWAITING_CONFIRMATION
    assert result.resulting_appointment_id is None
    new_starts = {o["start_at"] for o in result.offered_slots}
    assert original_offer["start_at"] not in new_starts

    steps = (
        (
            await db.execute(
                select(WorkflowStep.step_name)
                .where(WorkflowStep.processing_run_id == run.id)
                .order_by(WorkflowStep.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert "slot_no_longer_available" in steps

    # The appointment being rescheduled is untouched -- still at its
    # original time.
    await db.refresh(appointment)
    assert appointment.start_at != contested_start


@pytest.mark.asyncio
async def test_reschedule_intent_escalates_when_no_upcoming_appointment(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="I need to reschedule please"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.ESCALATED
    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert "no upcoming appointment" in case.reason


@pytest.mark.asyncio
async def test_reschedule_intent_escalates_when_multiple_upcoming_appointments(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    first = await _book(db, business, customer)
    second = await _book(db, business, customer)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="I need to reschedule please"
    )
    now = min(first.start_at, second.start_at) - timedelta(hours=48)
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    assert run.state == ProcessingRunState.ESCALATED
    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert "multiple upcoming appointments" in case.reason


@pytest.mark.asyncio
async def test_reschedule_intent_rejected_inside_notice_window(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)

    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="I need to reschedule please"
    )
    now = appointment.start_at - timedelta(hours=1)
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
        now=now,
    )

    assert run.state == ProcessingRunState.ESCALATED
    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert "notice window" in case.reason

    await db.refresh(appointment)
    assert appointment.status == AppointmentStatus.BOOKED
