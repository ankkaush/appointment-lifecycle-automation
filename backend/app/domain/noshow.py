"""The no-show rule: deterministic, rule-based, no inference. Designed
back in the architecture review (Concern 3) and implemented here for the
first time -- a pure predicate, no DB access, no side effects, mirroring
the style of availability.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.models import Appointment, AppointmentStatus, Business


def is_no_show(appointment: Appointment, business: Business, now: datetime) -> bool:
    """True once the appointment's end time plus the business's configured
    grace period has passed, and nothing has resolved it since. Staff
    marking an appointment COMPLETED/CANCELLED/NO_SHOW manually at any
    point -- before or after this would otherwise fire -- always takes
    precedence, simply because this predicate only ever looks at
    still-BOOKED appointments."""
    if appointment.status != AppointmentStatus.BOOKED:
        return False
    grace = timedelta(minutes=business.no_show_grace_period_minutes)
    return now > appointment.end_at + grace
