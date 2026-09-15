"""Cancellation and rescheduling: the two mutations that act on an
existing, already-BOOKED Appointment rather than creating a new one.

Rescheduling shares booking.py's two-layer concurrency discipline --a
deterministic availability re-check, backed by the real guarantee, the
same Postgres exclusion constraint -- without duplicating booking.py's
insert path, since this updates an existing row in place instead of
inserting a new one. `_RETRYABLE_BOOKING_SQLSTATES` is imported rather
than redefined: it's not booking-specific, it's the set of SQLSTATEs
that mean "the exclusion constraint (or a deadlock/serialization
conflict around it) rejected this write" for any writer touching this
constraint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import availability
from app.domain.booking import _RETRYABLE_BOOKING_SQLSTATES
from app.domain.exceptions import (
    AppointmentNotModifiableError,
    EntityNotFoundError,
    SlotUnavailableError,
)
from app.domain.models import Appointment, AppointmentStatus, Business, Service, StaffResource


def can_modify(appointment: Appointment, business: Business, now: datetime) -> bool:
    """Whether `appointment` may still be cancelled or rescheduled through
    the automated flow: it must still be BOOKED, and outside the
    business's shared notice window. One policy knob
    (Business.min_reschedule_notice_hours) for both actions, per the
    approved Phase 7 design -- cancelling and moving an appointment ask
    the same "how much notice do we need" question."""
    if appointment.status != AppointmentStatus.BOOKED:
        return False
    return now + timedelta(hours=business.min_reschedule_notice_hours) <= appointment.start_at


async def _get_or_404(db: AsyncSession, model: type, entity_id: UUID, name: str):
    obj = await db.get(model, entity_id)
    if obj is None:
        raise EntityNotFoundError(name, entity_id)
    return obj


async def cancel_appointment(
    db: AsyncSession, appointment_id: UUID, *, now: datetime | None = None
) -> Appointment:
    now = now or datetime.now(UTC)
    appointment = await _get_or_404(db, Appointment, appointment_id, "Appointment")
    business = await _get_or_404(db, Business, appointment.business_id, "Business")

    if not can_modify(appointment, business, now):
        raise AppointmentNotModifiableError(
            f"Appointment {appointment_id} cannot be cancelled: not BOOKED, or inside the "
            f"{business.min_reschedule_notice_hours}h cancellation notice window"
        )

    appointment.status = AppointmentStatus.CANCELLED
    await db.commit()
    await db.refresh(appointment)
    return appointment


async def reschedule_appointment(
    db: AsyncSession, appointment_id: UUID, new_start_at: datetime, *, now: datetime | None = None
) -> Appointment:
    """Moves `appointment_id` to `new_start_at` in place -- same row, same
    id, same history. Re-validates availability deterministically
    regardless of what was offered earlier, then relies on the exclusion
    constraint as the real guarantee against a concurrent conflict,
    exactly like booking.book_appointment."""
    now = now or datetime.now(UTC)
    appointment = await _get_or_404(db, Appointment, appointment_id, "Appointment")
    business = await _get_or_404(db, Business, appointment.business_id, "Business")

    if not can_modify(appointment, business, now):
        raise AppointmentNotModifiableError(
            f"Appointment {appointment_id} cannot be rescheduled: not BOOKED, or inside the "
            f"{business.min_reschedule_notice_hours}h reschedule notice window"
        )

    service = await _get_or_404(db, Service, appointment.service_id, "Service")
    staff = await _get_or_404(db, StaffResource, appointment.staff_id, "StaffResource")

    if not await availability.is_slot_available(
        db,
        business=business,
        service=service,
        staff=staff,
        start_at=new_start_at,
        now=now,
        exclude_appointment_id=appointment.id,
    ):
        raise SlotUnavailableError(
            f"{new_start_at.isoformat()} is not available for staff {staff.id}"
        )

    appointment.start_at = new_start_at
    appointment.end_at = new_start_at + timedelta(minutes=service.duration_minutes)
    try:
        await db.commit()
    except DBAPIError as exc:
        await db.rollback()
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate not in _RETRYABLE_BOOKING_SQLSTATES:
            raise
        raise SlotUnavailableError(
            f"{new_start_at.isoformat()} was booked by a concurrent request"
        ) from exc

    await db.refresh(appointment)
    return appointment
