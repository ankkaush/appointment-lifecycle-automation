"""The deterministic availability engine.

Availability is never a stored fact (architecture baseline, Concern 7) — it
is computed on demand from working hours, blocked periods, existing
bookings, and the business's notice/horizon policy. Nothing here is cached
or persisted as its own state, and nothing here is AI-influenced: this
module is pure computation over what's currently in Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    Appointment,
    AppointmentStatus,
    BlockedPeriod,
    Business,
    Service,
    StaffResource,
    StaffWorkingHours,
)


@dataclass(frozen=True)
class SlotCandidate:
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    def overlaps(self, other: TimeWindow) -> bool:
        return self.start < other.end and other.start < self.end


def _effective_window(start_at: datetime, end_at: datetime, buffer_minutes: int) -> TimeWindow:
    """What a booking actually occupies on staff's schedule, including the
    buffer reserved after it. See models.py's note on Service.buffer_minutes
    for why this buffer isn't also a database-level constraint."""
    return TimeWindow(start_at, end_at + timedelta(minutes=buffer_minutes))


def _subtract(windows: list[TimeWindow], blockers: list[TimeWindow]) -> list[TimeWindow]:
    """Remove every blocker window from every source window, returning the
    remaining free sub-windows."""
    free = list(windows)
    for blocker in blockers:
        next_free: list[TimeWindow] = []
        for w in free:
            if not w.overlaps(blocker):
                next_free.append(w)
                continue
            if blocker.start > w.start:
                next_free.append(TimeWindow(w.start, min(blocker.start, w.end)))
            if blocker.end < w.end:
                next_free.append(TimeWindow(max(blocker.end, w.start), w.end))
        free = next_free
    return free


async def _working_hours_windows(
    db: AsyncSession, staff_id, tz: ZoneInfo, bound: TimeWindow
) -> list[TimeWindow]:
    stmt = select(
        StaffWorkingHours.weekday, StaffWorkingHours.start_time, StaffWorkingHours.end_time
    ).where(StaffWorkingHours.staff_id == staff_id)
    rules = (await db.execute(stmt)).all()
    if not rules:
        return []

    by_weekday: dict[int, list[tuple]] = {}
    for weekday, start_time, end_time in rules:
        by_weekday.setdefault(weekday, []).append((start_time, end_time))

    windows: list[TimeWindow] = []
    day = bound.start.astimezone(tz).date()
    last_day = bound.end.astimezone(tz).date()
    while day <= last_day:
        for start_time, end_time in by_weekday.get(day.weekday(), []):
            # Built directly against the business's IANA zone for this
            # calendar date, so DST transitions resolve correctly.
            start_dt = datetime.combine(day, start_time, tzinfo=tz)
            end_dt = datetime.combine(day, end_time, tzinfo=tz)
            windows.append(TimeWindow(start_dt, end_dt))
        day += timedelta(days=1)
    return windows


async def _blocked_windows(
    db: AsyncSession, business_id, staff_id, bound: TimeWindow
) -> list[TimeWindow]:
    stmt = select(BlockedPeriod.start_at, BlockedPeriod.end_at).where(
        BlockedPeriod.business_id == business_id,
        (BlockedPeriod.staff_id == staff_id) | (BlockedPeriod.staff_id.is_(None)),
        BlockedPeriod.start_at < bound.end,
        BlockedPeriod.end_at > bound.start,
    )
    rows = (await db.execute(stmt)).all()
    return [TimeWindow(s, e) for s, e in rows]


async def _booked_windows(
    db: AsyncSession, staff_id, bound: TimeWindow, *, exclude_appointment_id: UUID | None = None
) -> list[TimeWindow]:
    stmt = (
        select(Appointment.start_at, Appointment.end_at, Service.buffer_minutes)
        .join(Service, Service.id == Appointment.service_id)
        .where(
            Appointment.staff_id == staff_id,
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.start_at < bound.end,
            Appointment.end_at > bound.start,
        )
    )
    if exclude_appointment_id is not None:
        # Rescheduling re-checks availability while the appointment being
        # moved is still sitting BOOKED at its *old* time -- without this,
        # a move to a nearby slot could be falsely rejected as colliding
        # with itself. The exclusion constraint (a pairwise, cross-row
        # guarantee) never has this problem; only this in-process
        # re-check does.
        stmt = stmt.where(Appointment.id != exclude_appointment_id)
    rows = (await db.execute(stmt)).all()
    return [_effective_window(s, e, b) for s, e, b in rows]


async def list_available_slots(
    db: AsyncSession,
    *,
    business: Business,
    service: Service,
    staff: StaffResource,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    exclude_appointment_id: UUID | None = None,
) -> list[SlotCandidate]:
    """Working hours, minus blocked periods, minus existing bookings (with
    buffer), clipped to the business's booking-notice / booking-horizon
    policy. Deterministic and side-effect-free: the same inputs always
    produce the same slots.

    `exclude_appointment_id` is for rescheduling only (app.domain.
    appointments.reschedule_appointment): it leaves the appointment being
    moved out of its own conflict check. Every other caller leaves it
    unset.
    """
    tz = ZoneInfo(business.timezone)
    duration = timedelta(minutes=service.duration_minutes)

    earliest = max(window_start, now + timedelta(minutes=business.min_booking_notice_minutes))
    latest = min(window_end, now + timedelta(days=business.max_booking_horizon_days))
    if earliest >= latest:
        return []

    bound = TimeWindow(earliest, latest)

    hours = await _working_hours_windows(db, staff.id, tz, bound)
    hours = [TimeWindow(max(w.start, bound.start), min(w.end, bound.end)) for w in hours]
    hours = [w for w in hours if w.start < w.end]

    blocked = await _blocked_windows(db, business.id, staff.id, bound)
    booked = await _booked_windows(
        db, staff.id, bound, exclude_appointment_id=exclude_appointment_id
    )
    free = _subtract(hours, blocked + booked)

    slots: list[SlotCandidate] = []
    for w in free:
        cursor = w.start
        while cursor + duration <= w.end:
            slots.append(SlotCandidate(cursor, cursor + duration))
            cursor += duration
    return slots


async def is_slot_available(
    db: AsyncSession,
    *,
    business: Business,
    service: Service,
    staff: StaffResource,
    start_at: datetime,
    now: datetime,
    exclude_appointment_id: UUID | None = None,
) -> bool:
    """The single-slot check the booking engine re-runs at commit time,
    regardless of what was offered earlier (architecture baseline,
    Section J: offered slots are never binding on their own)."""
    end_at = start_at + timedelta(minutes=service.duration_minutes)
    slots = await list_available_slots(
        db,
        business=business,
        service=service,
        staff=staff,
        window_start=start_at,
        window_end=end_at,
        now=now,
        exclude_appointment_id=exclude_appointment_id,
    )
    return any(s.start_at == start_at and s.end_at == end_at for s in slots)
