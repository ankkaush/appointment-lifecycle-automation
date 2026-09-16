"""Response shapes for the operator dashboard (Phase 11): read-only
projections over data the existing workflow/jobs code already writes.
Kept separate from workflow/schemas.py since these are dashboard-specific
views assembled from several tables, not core request/response shapes for
the workflow endpoints themselves.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AppointmentListItemOut(BaseModel):
    id: UUID
    status: str
    start_at: datetime
    end_at: datetime
    calendar_sync_status: str
    customer_id: UUID
    customer_name: str
    customer_contact: str
    service_name: str
    staff_name: str
    rebooked_from_id: UUID | None


class TimelineEntryOut(BaseModel):
    at: datetime
    label: str
    detail: str | None
    # Which existing table this entry came from -- WorkflowStep,
    # AuditEvent, Notification, or ScheduledJob. Not shown prominently in
    # the UI; useful for debugging/filtering.
    source: str


class AppointmentTimelineOut(BaseModel):
    appointment: AppointmentListItemOut
    entries: list[TimelineEntryOut]


class CustomerListItemOut(BaseModel):
    id: UUID
    name: str
    contact: str
    total_appointments: int
    upcoming_appointment_at: datetime | None
    latest_status: str | None


class NotificationListItemOut(BaseModel):
    id: UUID
    created_at: datetime
    customer_name: str
    appointment_id: UUID | None
    subject: str
    channel: str
    status: str


class ScheduledJobListItemOut(BaseModel):
    id: UUID
    job_type: str
    status: str
    run_at: datetime
    attempts: int
    last_error: str | None
    customer_name: str | None
    appointment_id: UUID | None


class ActivityEntryOut(BaseModel):
    at: datetime
    label: str
    detail: str | None
    source: str
    customer_name: str | None
    appointment_id: UUID | None


class OverviewSummaryOut(BaseModel):
    upcoming_appointments: int
    todays_appointments: int
    pending_reminders: int
    no_shows: int
    open_escalations: int
    notifications_sent: int
    notifications_failed: int
