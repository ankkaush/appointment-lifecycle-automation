"""The booking engine: the one write path that creates a BOOKED appointment.

Concurrency safety (architecture baseline, Section J) has two independent
layers here, deliberately not just one:

1. A deterministic re-check of availability at commit time, regardless of
   what was offered to the customer earlier — an offered slot is never
   binding on its own.
2. The database's own range-exclusion constraint on Appointment, which is
   the actual guarantee: it holds even if this function's own re-check has
   a bug, because it's enforced by Postgres itself, not by application
   discipline.

A duplicate idempotency_key — a retried confirm, a network retry — resolves
to the original appointment instead of erroring or double-booking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import availability
from app.domain.exceptions import (
    EntityNotFoundError,
    SlotUnavailableError,
    StaffCannotPerformServiceError,
)
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Service,
    StaffResource,
    staff_services,
)
from app.domain.schemas import BookingRequest


async def _get_or_404(db: AsyncSession, model: type, entity_id: UUID, name: str):
    obj = await db.get(model, entity_id)
    if obj is None:
        raise EntityNotFoundError(name, entity_id)
    return obj


async def _staff_can_perform(db: AsyncSession, staff_id: UUID, service_id: UUID) -> bool:
    stmt = select(staff_services.c.staff_id).where(
        staff_services.c.staff_id == staff_id, staff_services.c.service_id == service_id
    )
    return (await db.execute(stmt)).first() is not None


async def book_appointment(
    db: AsyncSession, request: BookingRequest, *, now: datetime | None = None
) -> Appointment:
    now = now or datetime.now(UTC)

    existing = await db.scalar(
        select(Appointment).where(Appointment.idempotency_key == request.idempotency_key)
    )
    if existing is not None:
        return existing

    business = await _get_or_404(db, Business, request.business_id, "Business")
    service = await _get_or_404(db, Service, request.service_id, "Service")
    staff = await _get_or_404(db, StaffResource, request.staff_id, "StaffResource")
    await _get_or_404(db, Customer, request.customer_id, "Customer")

    if not await _staff_can_perform(db, staff.id, service.id):
        raise StaffCannotPerformServiceError(
            f"Staff {staff.id} does not perform service {service.id}"
        )

    if not await availability.is_slot_available(
        db, business=business, service=service, staff=staff, start_at=request.start_at, now=now
    ):
        raise SlotUnavailableError(
            f"{request.start_at.isoformat()} is not available for staff {staff.id}"
        )

    end_at = request.start_at + timedelta(minutes=service.duration_minutes)
    appointment = Appointment(
        business_id=business.id,
        service_id=service.id,
        staff_id=staff.id,
        customer_id=request.customer_id,
        start_at=request.start_at,
        end_at=end_at,
        status=AppointmentStatus.BOOKED,
        idempotency_key=request.idempotency_key,
    )
    db.add(appointment)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Either the exclusion constraint caught a genuine race on the slot,
        # or a concurrent request landed with the same idempotency key
        # first. Distinguish and respond cleanly rather than surfacing a
        # raw database error to the caller.
        again = await db.scalar(
            select(Appointment).where(Appointment.idempotency_key == request.idempotency_key)
        )
        if again is not None:
            return again
        raise SlotUnavailableError(
            f"{request.start_at.isoformat()} was booked by a concurrent request"
        ) from exc

    await db.refresh(appointment)
    return appointment
