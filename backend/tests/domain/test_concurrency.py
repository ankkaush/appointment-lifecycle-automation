"""Proves the concurrency claim in the architecture baseline (Section J):
two booking attempts for the same staff member and overlapping time, fired
genuinely concurrently against real, independently-committing database
sessions, must never both succeed. This is not a unit test of application
logic — it is a test of the database's own exclusion constraint under a
real race, which is the actual guarantee the design relies on.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain import booking
from app.domain.exceptions import SlotUnavailableError
from app.domain.models import AppointmentStatus, Business, Customer, Service, StaffResource
from app.domain.schemas import BookingRequest


def _next_monday_9am_utc(business: Business) -> datetime:
    tz = ZoneInfo(business.timezone)
    today = datetime.now(tz).date()
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    local_9am = datetime.combine(monday, datetime.min.time(), tzinfo=tz).replace(hour=9)
    return local_9am.astimezone(UTC)


@pytest.mark.asyncio
async def test_concurrent_bookings_for_the_same_slot_only_one_wins(
    db,
    session_factory: async_sessionmaker,
    business: Business,
    service: Service,
    staff: StaffResource,
) -> None:
    customer_a = Customer(business_id=business.id, name="Customer A", contact="a@example.com")
    customer_b = Customer(business_id=business.id, name="Customer B", contact="b@example.com")
    db.add_all([customer_a, customer_b])
    await db.commit()
    await db.refresh(customer_a)
    await db.refresh(customer_b)

    start_at = _next_monday_9am_utc(business)

    async def attempt(customer_id):
        # Each attempt gets its own session/connection — genuinely
        # independent transactions racing on the same slot, not one
        # session serialized against itself.
        async with session_factory() as session:
            request = BookingRequest(
                idempotency_key=str(uuid4()),
                business_id=business.id,
                service_id=service.id,
                staff_id=staff.id,
                customer_id=customer_id,
                start_at=start_at,
            )
            try:
                return await booking.book_appointment(session, request)
            except SlotUnavailableError as exc:
                return exc

    results = await asyncio.gather(
        attempt(customer_a.id), attempt(customer_b.id), return_exceptions=False
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    rejections = [r for r in results if isinstance(r, SlotUnavailableError)]

    assert len(successes) == 1, "exactly one of the two concurrent requests should win the slot"
    assert len(rejections) == 1, "the other request should get a clean rejection, not a crash"

    async with session_factory() as verify:
        from sqlalchemy import select

        from app.domain.models import Appointment

        rows = (
            (
                await verify.execute(
                    select(Appointment).where(
                        Appointment.staff_id == staff.id,
                        Appointment.start_at == start_at,
                        Appointment.status == AppointmentStatus.BOOKED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "no duplicate booking should exist for the slot"
