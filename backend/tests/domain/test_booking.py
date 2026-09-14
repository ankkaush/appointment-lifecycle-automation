from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import booking
from app.domain.exceptions import (
    EntityNotFoundError,
    SlotUnavailableError,
    StaffCannotPerformServiceError,
)
from app.domain.models import AppointmentStatus, Business, Customer, Service, StaffResource
from app.domain.schemas import BookingRequest

# `customer` fixture comes from tests/conftest.py — shared across suites now
# that the workflow tests need it too.


def _next_monday_9am_utc(business: Business) -> datetime:
    tz = ZoneInfo(business.timezone)
    today = datetime.now(tz).date()
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    local_9am = datetime.combine(monday, datetime.min.time(), tzinfo=tz).replace(hour=9)
    return local_9am.astimezone(UTC)


@pytest.mark.asyncio
async def test_book_appointment_succeeds(
    db: AsyncSession,
    business: Business,
    service: Service,
    staff: StaffResource,
    customer: Customer,
    idempotency_key: str,
) -> None:
    start_at = _next_monday_9am_utc(business)
    request = BookingRequest(
        idempotency_key=idempotency_key,
        business_id=business.id,
        service_id=service.id,
        staff_id=staff.id,
        customer_id=customer.id,
        start_at=start_at,
    )

    appointment = await booking.book_appointment(db, request)

    assert appointment.status == AppointmentStatus.BOOKED
    assert appointment.start_at == start_at
    assert appointment.end_at == start_at + timedelta(minutes=service.duration_minutes)


@pytest.mark.asyncio
async def test_booking_already_taken_slot_is_rejected(
    db: AsyncSession,
    business: Business,
    service: Service,
    staff: StaffResource,
    customer: Customer,
) -> None:
    start_at = _next_monday_9am_utc(business)
    first = BookingRequest(
        idempotency_key=str(uuid4()),
        business_id=business.id,
        service_id=service.id,
        staff_id=staff.id,
        customer_id=customer.id,
        start_at=start_at,
    )
    await booking.book_appointment(db, first)

    second = BookingRequest(
        idempotency_key=str(uuid4()),
        business_id=business.id,
        service_id=service.id,
        staff_id=staff.id,
        customer_id=customer.id,
        start_at=start_at,  # same slot
    )
    with pytest.raises(SlotUnavailableError):
        await booking.book_appointment(db, second)


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_returns_same_appointment(
    db: AsyncSession,
    business: Business,
    service: Service,
    staff: StaffResource,
    customer: Customer,
    idempotency_key: str,
) -> None:
    start_at = _next_monday_9am_utc(business)
    request = BookingRequest(
        idempotency_key=idempotency_key,
        business_id=business.id,
        service_id=service.id,
        staff_id=staff.id,
        customer_id=customer.id,
        start_at=start_at,
    )

    first = await booking.book_appointment(db, request)
    # Simulate a client retry — same idempotency key, could even be a
    # different (buggy) start_at; the original booking wins either way.
    second = await booking.book_appointment(db, request)

    assert first.id == second.id


@pytest.mark.asyncio
async def test_staff_not_eligible_for_service_is_rejected(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    from app.domain.models import Service as ServiceModel
    from app.domain.models import StaffResource as StaffModel

    other_service = ServiceModel(business_id=business.id, name="Massage", duration_minutes=60)
    other_staff = StaffModel(business_id=business.id, name="Sam")
    db.add_all([other_service, other_staff])
    await db.commit()
    await db.refresh(other_service)
    await db.refresh(other_staff)
    # deliberately no staff_services row linking them

    request = BookingRequest(
        idempotency_key=str(uuid4()),
        business_id=business.id,
        service_id=other_service.id,
        staff_id=other_staff.id,
        customer_id=customer.id,
        start_at=_next_monday_9am_utc(business),
    )
    with pytest.raises(StaffCannotPerformServiceError):
        await booking.book_appointment(db, request)


@pytest.mark.asyncio
async def test_booking_unknown_business_raises_not_found(
    db: AsyncSession, service: Service, staff: StaffResource, customer: Customer
) -> None:
    request = BookingRequest(
        idempotency_key=str(uuid4()),
        business_id=uuid4(),
        service_id=service.id,
        staff_id=staff.id,
        customer_id=customer.id,
        start_at=datetime.now(UTC) + timedelta(days=1),
    )
    with pytest.raises(EntityNotFoundError):
        await booking.book_appointment(db, request)
