"""Background jobs: reminders, no-show detection + recovery outreach, and
expiring abandoned conversations. No live API calls -- FakeInterpreter and
Mock providers throughout, with `now` injected everywhere so nothing here
depends on wall-clock time.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.fake import FakeInterpreter
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Service,
    StaffResource,
)
from app.workflow import orchestrator
from app.workflow.calendar import MockCalendarProvider
from app.workflow.jobs import (
    AWAITING_REPLY_TIMEOUT_MINUTES,
    run_due_reminders,
    sweep_expired_runs,
    sweep_no_shows,
)
from app.workflow.models import (
    Notification,
    ProcessingRun,
    ProcessingRunKind,
    ProcessingRunState,
    ScheduledJob,
    ScheduledJobStatus,
    ScheduledJobType,
)
from app.workflow.notifications import MockNotificationProvider
from app.workflow.schemas import ConfirmSlotIn, StartRequestIn


async def _book(db: AsyncSession, business: Business, customer: Customer) -> Appointment:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Book me a Haircut please"
    )
    run = await orchestrator.start_request(db, payload, interpreter=FakeInterpreter())
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
    )
    return await db.get(Appointment, result.resulting_appointment_id)


# --- reminders -------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminder_job_scheduled_at_confirm_time(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)

    job = (
        await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.job_type == ScheduledJobType.SEND_REMINDER,
                ScheduledJob.payload["appointment_id"].astext == str(appointment.id),
            )
        )
    ).scalar_one()
    assert job.status == ScheduledJobStatus.PENDING
    assert job.run_at == appointment.start_at - timedelta(hours=business.reminder_lead_hours)


@pytest.mark.asyncio
async def test_due_reminder_is_sent(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    due_now = (
        appointment.start_at - timedelta(hours=business.reminder_lead_hours) + timedelta(seconds=1)
    )

    processed = await run_due_reminders(
        db, notification_service=MockNotificationProvider(), now=due_now
    )

    assert processed == 1
    job = (
        await db.execute(
            select(ScheduledJob).where(ScheduledJob.job_type == ScheduledJobType.SEND_REMINDER)
        )
    ).scalar_one()
    assert job.status == ScheduledJobStatus.DONE

    reminder = (
        await db.execute(
            select(Notification).where(
                Notification.appointment_id == appointment.id,
                Notification.subject == "Appointment reminder",
            )
        )
    ).scalar_one()
    assert reminder.to_contact == customer.contact


@pytest.mark.asyncio
async def test_reminder_job_not_yet_due_is_left_pending(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    await _book(db, business, customer)
    far_in_the_past = datetime(2020, 1, 1, tzinfo=UTC)

    processed = await run_due_reminders(
        db, notification_service=MockNotificationProvider(), now=far_in_the_past
    )
    assert processed == 0


@pytest.mark.asyncio
async def test_reminder_for_cancelled_appointment_is_a_noop(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    appointment.status = AppointmentStatus.CANCELLED
    await db.commit()

    due_now = (
        appointment.start_at - timedelta(hours=business.reminder_lead_hours) + timedelta(seconds=1)
    )
    processed = await run_due_reminders(
        db, notification_service=MockNotificationProvider(), now=due_now
    )

    assert processed == 1  # the job itself is still claimed and marked DONE...
    reminders = (
        (
            await db.execute(
                select(Notification).where(
                    Notification.appointment_id == appointment.id,
                    Notification.subject == "Appointment reminder",
                )
            )
        )
        .scalars()
        .all()
    )
    assert reminders == []  # ...but no reminder was actually sent


# --- no-show detection + recovery outreach ----------------------------------


@pytest.mark.asyncio
async def test_no_show_sweep_marks_appointment_and_opens_recovery(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    past_grace = appointment.end_at + timedelta(minutes=business.no_show_grace_period_minutes + 1)

    processed = await sweep_no_shows(
        db, notification_service=MockNotificationProvider(), now=past_grace
    )

    assert processed == 1
    await db.refresh(appointment)
    assert appointment.status == AppointmentStatus.NO_SHOW

    recovery_run = (
        await db.execute(
            select(ProcessingRun).where(ProcessingRun.recovery_of_appointment_id == appointment.id)
        )
    ).scalar_one()
    assert recovery_run.kind == ProcessingRunKind.RECOVERY
    assert recovery_run.state == ProcessingRunState.AWAITING_CLARIFICATION
    assert recovery_run.matched_service_id == appointment.service_id
    assert recovery_run.messages[-1]["role"] == "assistant"

    outreach = (
        await db.execute(
            select(Notification).where(
                Notification.appointment_id == appointment.id,
                Notification.subject == "We missed you",
            )
        )
    ).scalar_one()
    assert outreach.to_contact == customer.contact


@pytest.mark.asyncio
async def test_no_show_sweep_before_grace_period_is_a_noop(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    still_within_grace = appointment.end_at + timedelta(minutes=1)

    processed = await sweep_no_shows(
        db, notification_service=MockNotificationProvider(), now=still_within_grace
    )
    assert processed == 0
    await db.refresh(appointment)
    assert appointment.status == AppointmentStatus.BOOKED


@pytest.mark.asyncio
async def test_no_show_sweep_is_idempotent_on_repeated_runs(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    past_grace = appointment.end_at + timedelta(minutes=business.no_show_grace_period_minutes + 1)

    notifier = MockNotificationProvider()
    first = await sweep_no_shows(db, notification_service=notifier, now=past_grace)
    second = await sweep_no_shows(db, notification_service=notifier, now=past_grace)

    assert first == 1
    assert second == 0  # already NO_SHOW, not BOOKED -- nothing left to detect

    recovery_runs = (
        (
            await db.execute(
                select(ProcessingRun).where(
                    ProcessingRun.recovery_of_appointment_id == appointment.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(recovery_runs) == 1  # only one recovery attempt was ever opened


# --- expiring abandoned conversations ---------------------------------------


@pytest.mark.asyncio
async def test_expired_sweep_closes_stale_awaiting_confirmation(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Book me a Haircut please"
    )
    run = await orchestrator.start_request(db, payload, interpreter=FakeInterpreter())
    assert run.state == ProcessingRunState.AWAITING_CONFIRMATION

    # Refresh before reading updated_at: it's a server-generated onupdate
    # value, and this object went through several in-process flushes
    # already -- a bare attribute access here would trigger an implicit
    # lazy load outside the async greenlet context and raise
    # MissingGreenlet.
    await db.refresh(run)
    long_after = run.updated_at + timedelta(minutes=AWAITING_REPLY_TIMEOUT_MINUTES + 1)
    processed = await sweep_expired_runs(db, now=long_after)

    assert processed == 1
    await db.refresh(run)
    assert run.state == ProcessingRunState.EXPIRED


@pytest.mark.asyncio
async def test_expired_sweep_leaves_fresh_runs_alone(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Book me a Haircut please"
    )
    run = await orchestrator.start_request(db, payload, interpreter=FakeInterpreter())

    await db.refresh(run)
    soon_after = run.updated_at + timedelta(minutes=1)
    processed = await sweep_expired_runs(db, now=soon_after)

    assert processed == 0
    await db.refresh(run)
    assert run.state == ProcessingRunState.AWAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_expired_sweep_never_touches_terminal_runs(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book(db, business, customer)
    run = (
        await db.execute(
            select(ProcessingRun).where(ProcessingRun.resulting_appointment_id == appointment.id)
        )
    ).scalar_one()
    assert run.state == ProcessingRunState.SUCCEEDED

    far_future = run.updated_at + timedelta(days=1)
    processed = await sweep_expired_runs(db, now=far_future)

    assert processed == 0
    await db.refresh(run)
    assert run.state == ProcessingRunState.SUCCEEDED
