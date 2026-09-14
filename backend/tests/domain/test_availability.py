from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import availability
from app.domain.models import Appointment as AppointmentModel
from app.domain.models import AppointmentStatus, BlockedPeriod, Business, Service, StaffResource


def _next_monday_9am_utc(business: Business) -> datetime:
    """A Monday 09:00 local time, expressed in UTC, far enough in the
    future to be unambiguous regardless of when the test suite runs."""
    tz = ZoneInfo(business.timezone)
    today = datetime.now(tz).date()
    days_ahead = (7 - today.weekday()) % 7 or 7  # next Monday, never today
    monday = today + timedelta(days=days_ahead)
    local_9am = datetime.combine(monday, datetime.min.time(), tzinfo=tz).replace(hour=9)
    return local_9am.astimezone(UTC)


@pytest.mark.asyncio
async def test_slots_fall_within_working_hours(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource
) -> None:
    window_start = _next_monday_9am_utc(business)
    window_end = window_start + timedelta(hours=8)

    slots = await availability.list_available_slots(
        db,
        business=business,
        service=service,
        staff=staff,
        window_start=window_start,
        window_end=window_end,
        now=window_start - timedelta(days=1),
    )

    assert len(slots) > 0
    tz = ZoneInfo(business.timezone)
    for slot in slots:
        local_start = slot.start_at.astimezone(tz)
        local_end = slot.end_at.astimezone(tz)
        assert local_start.time() >= datetime.min.time().replace(hour=9)
        assert local_end.time() <= datetime.min.time().replace(hour=17)


@pytest.mark.asyncio
async def test_booked_appointment_removes_slot_including_buffer(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource
) -> None:
    window_start = _next_monday_9am_utc(business)
    window_end = window_start + timedelta(hours=2)

    booked_start = window_start
    booked_end = booked_start + timedelta(minutes=service.duration_minutes)
    db.add(
        AppointmentModel(
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=(await _make_customer(db, business)).id,
            start_at=booked_start,
            end_at=booked_end,
            status=AppointmentStatus.BOOKED,
            idempotency_key="seed-1",
        )
    )
    await db.commit()

    slots = await availability.list_available_slots(
        db,
        business=business,
        service=service,
        staff=staff,
        window_start=window_start,
        window_end=window_end,
        now=window_start - timedelta(days=1),
    )

    # service.duration=30, buffer=15 -> the booked slot plus its buffer
    # occupies 09:00-09:45, so the very next bookable slot starts at 09:45.
    assert all(s.start_at >= booked_end + timedelta(minutes=service.buffer_minutes) for s in slots)


@pytest.mark.asyncio
async def test_blocked_period_removes_slots(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource
) -> None:
    window_start = _next_monday_9am_utc(business)
    window_end = window_start + timedelta(hours=8)

    db.add(
        BlockedPeriod(
            business_id=business.id,
            staff_id=staff.id,
            start_at=window_start,
            end_at=window_start + timedelta(hours=8),
            reason="Day off",
        )
    )
    await db.commit()

    slots = await availability.list_available_slots(
        db,
        business=business,
        service=service,
        staff=staff,
        window_start=window_start,
        window_end=window_end,
        now=window_start - timedelta(days=1),
    )
    assert slots == []


@pytest.mark.asyncio
async def test_min_booking_notice_excludes_near_term_slots(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource
) -> None:
    business.min_booking_notice_minutes = 24 * 60
    db.add(business)
    await db.commit()

    window_start = _next_monday_9am_utc(business)
    window_end = window_start + timedelta(hours=8)

    slots = await availability.list_available_slots(
        db,
        business=business,
        service=service,
        staff=staff,
        window_start=window_start,
        window_end=window_end,
        now=window_start - timedelta(hours=1),  # inside the 24h notice window
    )
    assert slots == []


@pytest.mark.asyncio
async def test_max_booking_horizon_excludes_far_future_slots(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource
) -> None:
    business.max_booking_horizon_days = 1
    db.add(business)
    await db.commit()

    window_start = _next_monday_9am_utc(business)
    window_end = window_start + timedelta(hours=8)

    slots = await availability.list_available_slots(
        db,
        business=business,
        service=service,
        staff=staff,
        window_start=window_start,
        window_end=window_end,
        now=window_start - timedelta(days=30),  # far outside a 1-day horizon
    )
    assert slots == []


async def _make_customer(db: AsyncSession, business: Business):
    from app.domain.models import Customer

    obj = Customer(business_id=business.id, name="Test Customer", contact="test@example.com")
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj
