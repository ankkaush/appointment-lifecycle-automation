"""Domain schema: the persistent facts the availability engine and booking
engine compute from. This module has no knowledge of FastAPI, HTTP, or any
external vendor — it is pure SQLAlchemy over the entities the architecture
baseline defines as ours to own.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, String, Table, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AppointmentStatus(str, enum.Enum):
    """Final terminology from the approved baseline: the Appointment's own
    resource lifecycle, independent of whatever request produced it."""

    BOOKED = "BOOKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"


class CalendarSyncStatus(str, enum.Enum):
    """The calendar is an asynchronous mirror of the booking, never the
    source of truth -- this status is tracked independently of
    AppointmentStatus so a sync failure can never be confused with, or
    block, the booking itself (architecture baseline, Section H)."""

    PENDING = "PENDING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # IANA name (e.g. "America/New_York") — working hours are wall-clock
    # local time in this zone; everything persisted is UTC.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    min_booking_notice_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=60)
    max_booking_horizon_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=60)
    # How long past an appointment's end time it may sit BOOKED before the
    # no-show sweep resolves it -- the deterministic rule from the
    # architecture review's Concern 3: now > end_at + grace -> NO_SHOW.
    no_show_grace_period_minutes: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=15
    )
    # How long before an appointment's start time the reminder job fires.
    reminder_lead_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=24)
    # Shared policy for both cancellation and rescheduling (Phase 7): an
    # appointment inside this many hours of its start_at may not be
    # cancelled or moved through the automated flow -- one knob, not two,
    # since the two actions carry the same "how much notice do we need"
    # question.
    min_reschedule_notice_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=24)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=func.now()
    )

    services: Mapped[list[Service]] = relationship(back_populates="business")
    staff: Mapped[list[StaffResource]] = relationship(back_populates="business")


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # Gap reserved after this service before the same staff member's next
    # appointment may start. Enforced by the availability engine's
    # deterministic re-check — see the module docstring in booking.py for
    # why this is not also a database-level constraint.
    buffer_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=func.now()
    )

    business: Mapped[Business] = relationship(back_populates="services")

    __table_args__ = (
        sa.CheckConstraint("duration_minutes > 0", name="ck_service_duration_positive"),
        sa.CheckConstraint("buffer_minutes >= 0", name="ck_service_buffer_nonnegative"),
    )


class StaffResource(Base):
    __tablename__ = "staff_resources"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=func.now()
    )

    business: Mapped[Business] = relationship(back_populates="staff")


# Which staff members are eligible to perform which services. A plain
# many-to-many join table — this is configuration data, not workflow logic.
staff_services = Table(
    "staff_services",
    Base.metadata,
    Column(
        "staff_id",
        PGUUID(as_uuid=True),
        ForeignKey("staff_resources.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "service_id",
        PGUUID(as_uuid=True),
        ForeignKey("services.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class StaffWorkingHours(Base):
    """A recurring weekly working-hours rule for one staff member, in the
    business's local time. One row per (staff, weekday, window) — a staff
    member can have more than one window on the same day (e.g. split shift).
    """

    __tablename__ = "staff_working_hours"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    staff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_resources.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int] = mapped_column(sa.Integer, nullable=False)  # 0 = Monday .. 6 = Sunday
    start_time: Mapped[time] = mapped_column(sa.Time(timezone=False), nullable=False)
    end_time: Mapped[time] = mapped_column(sa.Time(timezone=False), nullable=False)

    __table_args__ = (
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_working_hours_weekday_range"),
        sa.CheckConstraint("start_time < end_time", name="ck_working_hours_order"),
    )


class BlockedPeriod(Base):
    """A holiday (staff_id NULL, applies business-wide) or a staff-specific
    block (time off, an ad-hoc closure). One concrete realization of the
    baseline's "AvailabilityRule" concept, alongside StaffWorkingHours.
    """

    __tablename__ = "blocked_periods"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    staff_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_resources.id", ondelete="CASCADE"), nullable=True
    )
    start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (sa.CheckConstraint("start_at < end_at", name="ck_blocked_period_order"),)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Single contact method by design — data minimization, per the
    # architecture baseline's security section, not an oversight.
    contact: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=func.now()
    )


class Appointment(Base):
    """The persistent business resource. Its lifecycle is independent of
    whatever request or workflow produced it — see the architecture
    baseline's Concern 1 correction. Phase 1 only ever creates rows in the
    BOOKED state; COMPLETED / CANCELLED / NO_SHOW transitions arrive in
    later phases.
    """

    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_resources.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )

    start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        sa.Enum(AppointmentStatus, name="appointment_status"),
        nullable=False,
        default=AppointmentStatus.BOOKED,
    )
    # Client-supplied key: a retried confirm, or a network retry, resolves
    # to the same appointment instead of creating a duplicate.
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # Calendar mirror state -- set after booking commits, never before,
    # and a FAILED sync never reverts status away from BOOKED. See
    # app/workflow/calendar.py.
    calendar_event_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    calendar_sync_status: Mapped[CalendarSyncStatus] = mapped_column(
        sa.Enum(CalendarSyncStatus, name="calendar_sync_status"),
        nullable=False,
        default=CalendarSyncStatus.PENDING,
    )
    # Set when this appointment was created by a successful no-show
    # recovery -- points at the original (which stays immutably NO_SHOW;
    # recovery never rewrites history, it creates a new row and links
    # back to it). Self-referential, deferred since Phase 1 for exactly
    # this moment.
    rebooked_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_appointment_idempotency_key"),
        sa.CheckConstraint("start_at < end_at", name="ck_appointment_time_order"),
        # The real concurrency guarantee (architecture baseline, Section J):
        # two BOOKED appointments for the same staff member cannot overlap,
        # enforced by Postgres itself — not by application-level checks.
        ExcludeConstraint(
            ("staff_id", "="),
            (text("tstzrange(start_at, end_at)"), "&&"),
            where=text("status = 'BOOKED'"),
            using="gist",
            name="ex_appointment_staff_no_overlap",
        ),
    )
