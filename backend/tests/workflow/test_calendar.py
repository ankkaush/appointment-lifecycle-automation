"""Calendar sync: best-effort, never blocking a booking that already
committed, always retryable and idempotent. No live network calls --
Mock/stub providers throughout.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.fake import FakeInterpreter
from app.domain.exceptions import EntityNotFoundError
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Business,
    CalendarSyncStatus,
    Customer,
    Service,
    StaffResource,
)
from app.workflow import orchestrator
from app.workflow.calendar import CalendarProviderError, MockCalendarProvider
from app.workflow.models import AuditEvent, Notification, ProcessingRunState, WorkflowStep
from app.workflow.notifications import MockNotificationProvider
from app.workflow.schemas import ConfirmSlotIn, StartRequestIn


@dataclass
class _FailingCalendarProvider:
    """Raises the documented contract exception -- the expected, handled
    shape of a calendar outage."""

    message: str = "simulated calendar outage"

    async def create_event(self, **kwargs) -> str:
        raise CalendarProviderError(self.message)

    async def update_event(self, **kwargs) -> None:
        raise CalendarProviderError(self.message)

    async def cancel_event(self, **kwargs) -> None:
        raise CalendarProviderError(self.message)


class _BrokenCalendarProvider:
    """Raises a plain, undocumented exception -- simulates a bug in a
    provider implementation, not an expected/handled failure. Used to
    prove a provider crash still can't corrupt the appointment's own
    state, since booking already committed before sync ever runs."""

    async def create_event(self, **kwargs) -> str:
        raise RuntimeError("boom")

    async def update_event(self, **kwargs) -> None:
        raise RuntimeError("boom")

    async def cancel_event(self, **kwargs) -> None:
        raise RuntimeError("boom")


async def _start_and_offer(db: AsyncSession, business: Business, customer: Customer):
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Book me a Haircut please"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    chosen = run.offered_slots[0]
    confirm_payload = ConfirmSlotIn(
        start_at=datetime.fromisoformat(chosen["start_at"]), idempotency_key=str(uuid4())
    )
    return run, confirm_payload


# --- confirm-time sync -----------------------------------------------------


@pytest.mark.asyncio
async def test_successful_calendar_sync_sets_event_id_and_status(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run, confirm_payload = await _start_and_offer(db, business, customer)

    result = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    appointment = await db.get(Appointment, result.resulting_appointment_id)
    assert appointment.calendar_sync_status == CalendarSyncStatus.SYNCED
    assert appointment.calendar_event_id == f"mock-{appointment.id}"
    assert appointment.status == AppointmentStatus.BOOKED

    steps = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "Appointment",
                    AuditEvent.entity_id == appointment.id,
                    AuditEvent.to_state == "calendar_sync:SYNCED",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(steps) == 1


@pytest.mark.asyncio
async def test_failed_calendar_sync_does_not_block_booking(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run, confirm_payload = await _start_and_offer(db, business, customer)

    result = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=_FailingCalendarProvider(),
    )

    # The ProcessingRun and the Appointment both still succeed -- a
    # calendar outage is not a booking failure.
    assert result.state == ProcessingRunState.SUCCEEDED
    assert result.resulting_appointment_id is not None

    appointment = await db.get(Appointment, result.resulting_appointment_id)
    assert appointment.status == AppointmentStatus.BOOKED
    assert appointment.calendar_sync_status == CalendarSyncStatus.FAILED
    assert appointment.calendar_event_id is None

    failure_events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "Appointment",
                    AuditEvent.entity_id == appointment.id,
                    AuditEvent.to_state == "calendar_sync:FAILED",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(failure_events) == 1
    assert failure_events[0].reason == "simulated calendar outage"


@pytest.mark.asyncio
async def test_notification_still_fires_when_calendar_sync_fails(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run, confirm_payload = await _start_and_offer(db, business, customer)

    await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=_FailingCalendarProvider(),
    )

    notification = (
        await db.execute(select(Notification).where(Notification.processing_run_id == run.id))
    ).scalar_one()
    assert notification.to_contact == customer.contact

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
    # calendar sync is attempted (and logged as failed) before the
    # confirmation step, but never replaces or skips it.
    assert "calendar_sync_failed" in steps
    assert "confirmation_sent" in steps
    assert steps.index("confirmation_sent") > steps.index("calendar_sync_failed")


@pytest.mark.asyncio
async def test_provider_exception_does_not_corrupt_appointment_state(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run, confirm_payload = await _start_and_offer(db, business, customer)

    with pytest.raises(RuntimeError):
        await orchestrator.confirm_slot(
            db,
            run.id,
            confirm_payload,
            notification_service=MockNotificationProvider(),
            calendar_provider=_BrokenCalendarProvider(),
        )

    # book_appointment committed the appointment in its own transaction
    # before calendar sync ever ran -- an unhandled provider bug
    # afterward cannot un-book it. Looked up independently by (staff,
    # start_at) rather than trusting in-memory state after the crash.
    appointment = (
        await db.execute(
            select(Appointment).where(
                Appointment.staff_id == staff.id,
                Appointment.start_at == confirm_payload.start_at,
            )
        )
    ).scalar_one()
    assert appointment.status == AppointmentStatus.BOOKED
    assert appointment.calendar_sync_status == CalendarSyncStatus.PENDING
    assert appointment.calendar_event_id is None


# --- manual retry ------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_calendar_sync_moves_failed_to_synced(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run, confirm_payload = await _start_and_offer(db, business, customer)
    confirmed = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=_FailingCalendarProvider(),
    )
    appointment_id = confirmed.resulting_appointment_id
    before = await db.get(Appointment, appointment_id)
    assert before.calendar_sync_status == CalendarSyncStatus.FAILED

    retried = await orchestrator.retry_calendar_sync(
        db, appointment_id, calendar_provider=MockCalendarProvider()
    )

    assert retried.calendar_sync_status == CalendarSyncStatus.SYNCED
    assert retried.calendar_event_id == f"mock-{appointment_id}"
    assert retried.status == AppointmentStatus.BOOKED


@pytest.mark.asyncio
async def test_retry_when_already_synced_is_a_harmless_noop(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run, confirm_payload = await _start_and_offer(db, business, customer)
    confirmed = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    appointment_id = confirmed.resulting_appointment_id
    original = await db.get(Appointment, appointment_id)
    assert original.calendar_sync_status == CalendarSyncStatus.SYNCED
    original_event_id = original.calendar_event_id

    # A provider that would raise if actually called -- proves retry on
    # an already-SYNCED appointment never touches the provider at all.
    retried = await orchestrator.retry_calendar_sync(
        db, appointment_id, calendar_provider=_FailingCalendarProvider()
    )

    assert retried.calendar_sync_status == CalendarSyncStatus.SYNCED
    assert retried.calendar_event_id == original_event_id


@pytest.mark.asyncio
async def test_retry_calendar_sync_unknown_appointment_raises(db: AsyncSession) -> None:
    with pytest.raises(EntityNotFoundError):
        await orchestrator.retry_calendar_sync(
            db, uuid4(), calendar_provider=MockCalendarProvider()
        )


@pytest.mark.asyncio
async def test_retry_still_failing_records_another_failure_without_corrupting_status(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run, confirm_payload = await _start_and_offer(db, business, customer)
    confirmed = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=_FailingCalendarProvider(),
    )
    appointment_id = confirmed.resulting_appointment_id

    retried = await orchestrator.retry_calendar_sync(
        db, appointment_id, calendar_provider=_FailingCalendarProvider("still down")
    )

    assert retried.calendar_sync_status == CalendarSyncStatus.FAILED
    assert retried.status == AppointmentStatus.BOOKED

    failure_events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "Appointment",
                    AuditEvent.entity_id == appointment_id,
                    AuditEvent.to_state == "calendar_sync:FAILED",
                )
            )
        )
        .scalars()
        .all()
    )
    # Once from the initial confirm-time failure, once from this retry.
    assert len(failure_events) == 2
