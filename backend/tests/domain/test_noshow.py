from datetime import UTC, datetime, timedelta

from app.domain.models import AppointmentStatus
from app.domain.noshow import is_no_show


class _FakeAppointment:
    def __init__(self, status: AppointmentStatus, end_at: datetime) -> None:
        self.status = status
        self.end_at = end_at


class _FakeBusiness:
    def __init__(self, grace_minutes: int) -> None:
        self.no_show_grace_period_minutes = grace_minutes


def test_not_no_show_before_grace_period_elapses() -> None:
    end_at = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    appointment = _FakeAppointment(AppointmentStatus.BOOKED, end_at)
    business = _FakeBusiness(grace_minutes=15)
    now = end_at + timedelta(minutes=10)
    assert is_no_show(appointment, business, now) is False


def test_no_show_after_grace_period_elapses() -> None:
    end_at = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    appointment = _FakeAppointment(AppointmentStatus.BOOKED, end_at)
    business = _FakeBusiness(grace_minutes=15)
    now = end_at + timedelta(minutes=16)
    assert is_no_show(appointment, business, now) is True


def test_exactly_at_grace_boundary_is_not_yet_a_no_show() -> None:
    end_at = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    appointment = _FakeAppointment(AppointmentStatus.BOOKED, end_at)
    business = _FakeBusiness(grace_minutes=15)
    now = end_at + timedelta(minutes=15)
    assert is_no_show(appointment, business, now) is False


def test_non_booked_appointment_is_never_a_no_show() -> None:
    end_at = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    business = _FakeBusiness(grace_minutes=15)
    now = end_at + timedelta(days=1)
    resolved_statuses = (
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.NO_SHOW,
    )
    for status in resolved_statuses:
        appointment = _FakeAppointment(status, end_at)
        assert is_no_show(appointment, business, now) is False
