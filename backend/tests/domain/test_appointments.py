import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import appointments, booking
from app.domain.exceptions import AppointmentNotModifiableError, SlotUnavailableError
from app.domain.models import AppointmentStatus, Business, Customer, Service, StaffResource
from app.domain.schemas import BookingRequest


def _next_monday_9am_utc(business: Business) -> datetime:
    tz = ZoneInfo(business.timezone)
    today = datetime.now(tz).date()
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    local_9am = datetime.combine(monday, datetime.min.time(), tzinfo=tz).replace(hour=9)
    return local_9am.astimezone(UTC)


class _FakeAppointment:
    def __init__(self, status: AppointmentStatus, start_at: datetime) -> None:
        self.status = status
        self.start_at = start_at


class _FakeBusiness:
    def __init__(self, notice_hours: int) -> None:
        self.min_reschedule_notice_hours = notice_hours


# --- can_modify (pure predicate) --------------------------------------------


def test_can_modify_true_when_booked_and_outside_notice_window() -> None:
    start_at = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    appointment = _FakeAppointment(AppointmentStatus.BOOKED, start_at)
    business = _FakeBusiness(notice_hours=24)
    now = start_at - timedelta(hours=25)
    assert appointments.can_modify(appointment, business, now) is True


def test_can_modify_false_inside_notice_window() -> None:
    start_at = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    appointment = _FakeAppointment(AppointmentStatus.BOOKED, start_at)
    business = _FakeBusiness(notice_hours=24)
    now = start_at - timedelta(hours=23)
    assert appointments.can_modify(appointment, business, now) is False


def test_can_modify_exactly_at_notice_boundary_is_still_allowed() -> None:
    start_at = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    appointment = _FakeAppointment(AppointmentStatus.BOOKED, start_at)
    business = _FakeBusiness(notice_hours=24)
    now = start_at - timedelta(hours=24)
    assert appointments.can_modify(appointment, business, now) is True


def test_can_modify_false_for_non_booked_appointment() -> None:
    start_at = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    business = _FakeBusiness(notice_hours=24)
    now = start_at - timedelta(days=30)
    for status in (
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.NO_SHOW,
    ):
        appointment = _FakeAppointment(status, start_at)
        assert appointments.can_modify(appointment, business, now) is False


# --- cancel_appointment ------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_appointment_succeeds_outside_notice_window(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    start_at = _next_monday_9am_utc(business)
    now = start_at - timedelta(hours=48)
    booked = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer.id,
            start_at=start_at,
        ),
        now=now,
    )

    cancelled = await appointments.cancel_appointment(db, booked.id, now=now)

    assert cancelled.id == booked.id
    assert cancelled.status == AppointmentStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_appointment_rejected_inside_notice_window(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    start_at = _next_monday_9am_utc(business)
    booked = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer.id,
            start_at=start_at,
        ),
        now=start_at - timedelta(hours=48),
    )

    now_too_close = start_at - timedelta(hours=1)
    with pytest.raises(AppointmentNotModifiableError):
        await appointments.cancel_appointment(db, booked.id, now=now_too_close)

    await db.refresh(booked)
    assert booked.status == AppointmentStatus.BOOKED


@pytest.mark.asyncio
async def test_cancel_appointment_rejected_when_already_cancelled(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    start_at = _next_monday_9am_utc(business)
    now = start_at - timedelta(hours=48)
    booked = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer.id,
            start_at=start_at,
        ),
        now=now,
    )
    await appointments.cancel_appointment(db, booked.id, now=now)

    with pytest.raises(AppointmentNotModifiableError):
        await appointments.cancel_appointment(db, booked.id, now=now)


# --- reschedule_appointment ---------------------------------------------------


@pytest.mark.asyncio
async def test_reschedule_appointment_moves_same_row_to_new_time(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    start_at = _next_monday_9am_utc(business)
    now = start_at - timedelta(hours=48)
    booked = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer.id,
            start_at=start_at,
        ),
        now=now,
    )

    new_start_at = start_at + timedelta(hours=2)
    rescheduled = await appointments.reschedule_appointment(db, booked.id, new_start_at, now=now)

    assert rescheduled.id == booked.id  # same appointment, same history
    assert rescheduled.start_at == new_start_at
    assert rescheduled.end_at == new_start_at + timedelta(minutes=service.duration_minutes)
    assert rescheduled.status == AppointmentStatus.BOOKED


@pytest.mark.asyncio
async def test_reschedule_appointment_to_a_slot_close_to_its_own_old_time_succeeds(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    """Regression case for the availability re-check: without excluding
    the appointment's own current (pre-move) row from the conflict check,
    moving it just past its own buffer would falsely collide with itself."""
    start_at = _next_monday_9am_utc(business)
    now = start_at - timedelta(hours=48)
    booked = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer.id,
            start_at=start_at,
        ),
        now=now,
    )

    # service.duration_minutes=30, buffer_minutes=15 (conftest fixture) --
    # the appointment's own old occupied+buffer window is [start, start+45m).
    # Moving to start+20m genuinely overlaps that window, so this only
    # succeeds if the appointment is excluded from its own conflict check.
    new_start_at = start_at + timedelta(minutes=20)
    rescheduled = await appointments.reschedule_appointment(db, booked.id, new_start_at, now=now)
    assert rescheduled.start_at == new_start_at


@pytest.mark.asyncio
async def test_reschedule_appointment_rejected_inside_notice_window(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    start_at = _next_monday_9am_utc(business)
    booked = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer.id,
            start_at=start_at,
        ),
        now=start_at - timedelta(hours=48),
    )

    now_too_close = start_at - timedelta(hours=1)
    with pytest.raises(AppointmentNotModifiableError):
        await appointments.reschedule_appointment(
            db, booked.id, start_at + timedelta(hours=2), now=now_too_close
        )

    await db.refresh(booked)
    assert booked.start_at == start_at  # untouched


@pytest.mark.asyncio
async def test_reschedule_appointment_rejected_when_new_slot_unavailable(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    start_at = _next_monday_9am_utc(business)
    now = start_at - timedelta(hours=48)
    booked = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer.id,
            start_at=start_at,
        ),
        now=now,
    )
    other_customer = Customer(business_id=business.id, name="Other", contact="other@example.com")
    db.add(other_customer)
    await db.flush()
    taken_start = start_at + timedelta(hours=3)
    await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=other_customer.id,
            start_at=taken_start,
        ),
        now=now,
    )

    with pytest.raises(SlotUnavailableError):
        await appointments.reschedule_appointment(db, booked.id, taken_start, now=now)

    await db.refresh(booked)
    assert booked.start_at == start_at  # untouched


@pytest.mark.asyncio
async def test_concurrent_reschedules_into_the_same_new_slot_only_one_wins(
    db: AsyncSession,
    session_factory,
    business: Business,
    service: Service,
    staff: StaffResource,
) -> None:
    """The real concurrency guarantee for rescheduling: the same exclusion
    constraint booking.py relies on, exercised against two genuinely
    independent, concurrently-committing sessions."""
    customer_a = Customer(business_id=business.id, name="Customer A", contact="a2@example.com")
    customer_b = Customer(business_id=business.id, name="Customer B", contact="b2@example.com")
    db.add_all([customer_a, customer_b])
    await db.commit()
    await db.refresh(customer_a)
    await db.refresh(customer_b)

    start_a = _next_monday_9am_utc(business)
    start_b = start_a + timedelta(hours=3)
    now = start_a - timedelta(hours=48)

    appointment_a = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer_a.id,
            start_at=start_a,
        ),
        now=now,
    )
    appointment_b = await booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=customer_b.id,
            start_at=start_b,
        ),
        now=now,
    )

    target_start = start_a + timedelta(hours=6)

    async def attempt(appointment_id):
        async with session_factory() as session:
            try:
                return await appointments.reschedule_appointment(
                    session, appointment_id, target_start, now=now
                )
            except SlotUnavailableError as exc:
                return exc

    results = await asyncio.gather(
        attempt(appointment_a.id), attempt(appointment_b.id), return_exceptions=False
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    rejections = [r for r in results if isinstance(r, SlotUnavailableError)]

    assert len(successes) == 1, "exactly one of the two concurrent reschedules should win"
    assert len(rejections) == 1, "the other should get a clean rejection, not a crash"

    from sqlalchemy import select

    from app.domain.models import Appointment

    async with session_factory() as verify:
        rows = (
            (
                await verify.execute(
                    select(Appointment).where(
                        Appointment.staff_id == staff.id,
                        Appointment.start_at == target_start,
                        Appointment.status == AppointmentStatus.BOOKED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "no duplicate booking should exist for the target slot"
