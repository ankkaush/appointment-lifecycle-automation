"""The calendar boundary: workflow code depends on CalendarProvider, never
on a specific vendor SDK. MockCalendarProvider is the only implementation
until a real adapter (e.g. Google Calendar) is added -- that's a new
module here, not new call sites, matching the same pattern as
app.ai.interpreter.Interpreter and app.workflow.notifications.

The calendar is an asynchronous mirror of a booking, never its source of
truth (architecture baseline, Section H). Postgres remains authoritative:
a booking that commits stays BOOKED regardless of what this provider
does. Three methods, nothing more.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.models import Appointment, Business, Customer, Service, StaffResource


class CalendarProviderError(Exception):
    """Raised when a calendar provider call fails. Callers must treat this
    as a sync failure to record and retry later -- never as a reason to
    invalidate the booking itself."""


class CalendarProvider(Protocol):
    async def create_event(
        self,
        *,
        appointment: Appointment,
        business: Business,
        service: Service,
        staff: StaffResource,
        customer: Customer,
    ) -> str:
        """Creates the external event and returns the provider's event id."""
        ...

    async def update_event(
        self,
        *,
        calendar_event_id: str,
        appointment: Appointment,
        business: Business,
        service: Service,
        staff: StaffResource,
        customer: Customer,
    ) -> None:
        """Called by the Phase 7 reschedule flow to mirror the appointment's
        new time. Best-effort, like create_event: a failure here is
        recorded on the appointment's calendar_sync_status and never
        undoes the reschedule itself, which has already committed in
        Postgres."""
        ...

    async def cancel_event(self, *, calendar_event_id: str) -> None:
        """Called by the Phase 7 cancellation flow. Same best-effort
        discipline as update_event -- a failure here never undoes the
        cancellation itself."""
        ...


class MockCalendarProvider:
    """Generates a fake event id and nothing else -- no real event is
    created anywhere. Used by default and in every test, exactly like
    MockNotificationProvider and FakeInterpreter."""

    async def create_event(
        self,
        *,
        appointment: Appointment,
        business: Business,
        service: Service,
        staff: StaffResource,
        customer: Customer,
    ) -> str:
        return f"mock-{appointment.id}"

    async def update_event(
        self,
        *,
        calendar_event_id: str,
        appointment: Appointment,
        business: Business,
        service: Service,
        staff: StaffResource,
        customer: Customer,
    ) -> None:
        return None

    async def cancel_event(self, *, calendar_event_id: str) -> None:
        return None
