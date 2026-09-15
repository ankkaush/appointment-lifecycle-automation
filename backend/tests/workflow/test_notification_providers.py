"""Phase 8: the real (non-mock) notification providers. Console never
touches the network at all; Resend is exercised only against an
httpx.MockTransport, matching the standing rule against live vendor calls
in pytest -- both the success path (recorded SENT) and the failure path
(recorded FAILED, never raised) are the thing actually worth testing here,
since orchestrator.py/jobs.py never change their own behavior either way.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Service,
    StaffResource,
)
from app.workflow.models import (
    Notification,
    NotificationStatus,
    ProcessingRun,
    ProcessingRunKind,
    ProcessingRunState,
)
from app.workflow.notification_dependency import select_notification_provider
from app.workflow.notifications import ConsoleNotificationProvider, MockNotificationProvider
from app.workflow.notifications_resend import ResendNotificationProvider


async def _make_run(db: AsyncSession, business: Business, customer: Customer) -> ProcessingRun:
    run = ProcessingRun(
        business_id=business.id,
        customer_id=customer.id,
        raw_message="test",
        state=ProcessingRunState.SUCCEEDED,
        kind=ProcessingRunKind.CUSTOMER_INITIATED,
        messages=[],
    )
    db.add(run)
    await db.flush()
    return run


async def _make_appointment(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> Appointment:
    start_at = datetime.now(UTC) + timedelta(days=1)
    appointment = Appointment(
        business_id=business.id,
        service_id=service.id,
        staff_id=staff.id,
        customer_id=customer.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=service.duration_minutes),
        status=AppointmentStatus.BOOKED,
        idempotency_key=str(uuid4()),
    )
    db.add(appointment)
    await db.flush()
    return appointment


# --- ConsoleNotificationProvider --------------------------------------------


@pytest.mark.asyncio
async def test_console_provider_persists_confirmation(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run = await _make_run(db, business, customer)
    appointment = await _make_appointment(db, business, service, staff, customer)

    notification = await ConsoleNotificationProvider().send_confirmation(
        db, appointment=appointment, customer=customer, run=run
    )

    assert notification.channel == "console"
    assert notification.status == NotificationStatus.SENT
    assert notification.subject == "Appointment confirmed"
    assert notification.to_contact == customer.contact


@pytest.mark.asyncio
async def test_console_provider_reminder_has_no_processing_run(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _make_appointment(db, business, service, staff, customer)

    notification = await ConsoleNotificationProvider().send_reminder(
        db, appointment=appointment, customer=customer
    )

    assert notification.processing_run_id is None
    assert notification.subject == "Appointment reminder"


# --- ResendNotificationProvider ---------------------------------------------


@pytest.mark.asyncio
async def test_resend_provider_success_records_sent(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run = await _make_run(db, business, customer)
    appointment = await _make_appointment(db, business, service, staff, customer)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.resend.com/emails"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"id": "email-123"})

    provider = ResendNotificationProvider(
        api_key="test-key",
        from_email="bookings@example.com",
        transport=httpx.MockTransport(handler),
    )

    notification = await provider.send_confirmation(
        db, appointment=appointment, customer=customer, run=run
    )

    assert notification.status == NotificationStatus.SENT
    assert notification.channel == "email"
    assert notification.to_contact == customer.contact


@pytest.mark.asyncio
async def test_resend_provider_failure_records_failed_without_raising(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run = await _make_run(db, business, customer)
    appointment = await _make_appointment(db, business, service, staff, customer)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "invalid `to` field"})

    provider = ResendNotificationProvider(
        api_key="test-key",
        from_email="bookings@example.com",
        transport=httpx.MockTransport(handler),
    )

    notification = await provider.send_cancellation(
        db, appointment=appointment, customer=customer, run=run
    )

    assert notification.status == NotificationStatus.FAILED
    assert notification.channel == "email"
    # The row still records what would have been sent -- useful for a
    # human reviewing why a customer never got their cancellation email.
    assert notification.subject == "Appointment cancelled"


@pytest.mark.asyncio
async def test_resend_provider_network_error_records_failed_without_raising(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run = await _make_run(db, business, customer)
    appointment = await _make_appointment(db, business, service, staff, customer)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    provider = ResendNotificationProvider(
        api_key="test-key",
        from_email="bookings@example.com",
        transport=httpx.MockTransport(handler),
    )

    notification = await provider.send_reschedule_confirmation(
        db, appointment=appointment, customer=customer, run=run
    )

    assert notification.status == NotificationStatus.FAILED


@pytest.mark.asyncio
async def test_resend_provider_persists_row_on_success_and_failure(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    run = await _make_run(db, business, customer)
    appointment = await _make_appointment(db, business, service, staff, customer)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "email-456"})

    provider = ResendNotificationProvider(
        api_key="test-key",
        from_email="bookings@example.com",
        transport=httpx.MockTransport(handler),
    )
    await provider.send_no_show_recovery(db, appointment=appointment, customer=customer, run=run)

    rows = (
        (
            await db.execute(
                select(Notification).where(Notification.appointment_id == appointment.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].subject == "We missed you"


# --- select_notification_provider (composition root logic) -----------------


def test_select_console_provider() -> None:
    settings = Settings(notification_provider="console")
    assert isinstance(select_notification_provider(settings), ConsoleNotificationProvider)


def test_select_mock_provider() -> None:
    settings = Settings(notification_provider="mock")
    assert isinstance(select_notification_provider(settings), MockNotificationProvider)


def test_select_resend_provider_with_credentials() -> None:
    settings = Settings(
        notification_provider="resend",
        resend_api_key="key-123",
        resend_from_email="bookings@example.com",
    )
    provider = select_notification_provider(settings)
    assert isinstance(provider, ResendNotificationProvider)


def test_select_resend_provider_without_credentials_raises() -> None:
    settings = Settings(notification_provider="resend", resend_api_key=None, resend_from_email=None)
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        select_notification_provider(settings)


def test_select_resend_provider_missing_from_email_raises() -> None:
    settings = Settings(
        notification_provider="resend", resend_api_key="key-123", resend_from_email=None
    )
    with pytest.raises(RuntimeError, match="RESEND_FROM_EMAIL"):
        select_notification_provider(settings)


def test_select_unknown_provider_raises() -> None:
    settings = Settings(notification_provider="carrier-pigeon")
    with pytest.raises(RuntimeError, match="Unknown NOTIFICATION_PROVIDER"):
        select_notification_provider(settings)
