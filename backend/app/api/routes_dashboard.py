"""HTTP boundary for the operator dashboard (Phase 11). Every endpoint
here is read-only and assembles from data the existing workflow/jobs code
already writes -- app.domain.booking, app.workflow.orchestrator, and
app.workflow.jobs are all untouched by this router. Same auth discipline
as the rest of the business-scoped API: require_business_api_key on
every endpoint.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_business_api_key
from app.core.db import get_db
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Customer,
    Service,
    StaffResource,
)
from app.workflow.dashboard_schemas import (
    ActivityEntryOut,
    AppointmentListItemOut,
    AppointmentTimelineOut,
    CustomerListItemOut,
    NotificationListItemOut,
    OverviewSummaryOut,
    ScheduledJobListItemOut,
    TimelineEntryOut,
)
from app.workflow.models import (
    AuditEvent,
    EscalationCase,
    EscalationStatus,
    Notification,
    NotificationStatus,
    ProcessingRun,
    ScheduledJob,
    ScheduledJobStatus,
    ScheduledJobType,
    WorkflowStep,
)

router = APIRouter()

# Human-readable labels for every WorkflowStep.step_name string this
# codebase actually writes (cataloged directly from orchestrator.py and
# jobs.py, not guessed) -- an unrecognized step_name falls back to the
# raw value rather than silently dropping the entry.
_STEP_LABELS: dict[str, str] = {
    "received": "Customer request received",
    "interpreted": "AI interpreted the request",
    "clarification_requested": "Clarifying question asked",
    "clarification_reply_received": "Customer replied to clarification",
    "slots_offered": "Available slots offered",
    "awaiting_confirmation": "Awaiting customer confirmation",
    "booking_attempted": "Booking attempted",
    "booking_succeeded": "Appointment booked",
    "slot_no_longer_available": "Chosen slot no longer available -- re-offering",
    "reschedule_attempted": "Reschedule attempted",
    "reschedule_succeeded": "Appointment rescheduled",
    "reschedule_confirmation_sent": "Reschedule confirmation sent",
    "cancellation_succeeded": "Appointment cancelled",
    "cancellation_notice_sent": "Cancellation notice sent",
    "calendar_sync_succeeded": "Calendar sync succeeded",
    "calendar_sync_failed": "Calendar sync failed",
    "calendar_cancel_succeeded": "Calendar event cancelled",
    "calendar_cancel_failed": "Calendar cancellation failed",
    "confirmation_sent": "Confirmation notice sent",
    "reminder_scheduled": "Reminder scheduled",
    "no_show_recovery_opened": "No-show recovery opened",
    "recovery_outreach_sent": "Recovery outreach sent",
    "escalated": "Escalated to a human",
    "interpretation_failed": "AI interpretation failed",
    "expired": "Conversation expired (no reply)",
}


def _appointment_out(
    appointment: Appointment, customer: Customer, service: Service, staff: StaffResource
) -> AppointmentListItemOut:
    return AppointmentListItemOut(
        id=appointment.id,
        status=appointment.status.value,
        start_at=appointment.start_at,
        end_at=appointment.end_at,
        calendar_sync_status=appointment.calendar_sync_status.value,
        customer_id=customer.id,
        customer_name=customer.name,
        customer_contact=customer.contact,
        service_name=service.name,
        staff_name=staff.name,
        rebooked_from_id=appointment.rebooked_from_id,
    )


def _joined_appointments_stmt(business_id: UUID):
    return (
        select(Appointment, Customer, Service, StaffResource)
        .join(Customer, Customer.id == Appointment.customer_id)
        .join(Service, Service.id == Appointment.service_id)
        .join(StaffResource, StaffResource.id == Appointment.staff_id)
        .where(Appointment.business_id == business_id)
    )


@router.get("/appointments", response_model=list[AppointmentListItemOut])
async def list_appointments(
    business_id: UUID,
    status_filter: AppointmentStatus | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> list[AppointmentListItemOut]:
    await require_business_api_key(db, business_id, authorization)
    stmt = _joined_appointments_stmt(business_id).order_by(Appointment.start_at)
    if status_filter is not None:
        stmt = stmt.where(Appointment.status == status_filter)
    if window_start is not None:
        stmt = stmt.where(Appointment.start_at >= window_start)
    if window_end is not None:
        stmt = stmt.where(Appointment.start_at < window_end)
    rows = (await db.execute(stmt)).all()
    return [_appointment_out(appt, cust, svc, staff) for appt, cust, svc, staff in rows]


def _audit_label(event: AuditEvent) -> str:
    # Most AuditEvents already carry a human-readable reason (see
    # orchestrator.py) -- fall back to formatting to_state only for the
    # one case that doesn't (the original calendar-sync success, which
    # has reason=None).
    if event.reason:
        return event.reason
    if event.to_state.startswith("calendar_sync:"):
        return f"Calendar sync: {event.to_state.split(':', 1)[1]}"
    return f"Status: {event.to_state}"


@router.get("/appointments/{appointment_id}/timeline", response_model=AppointmentTimelineOut)
async def get_appointment_timeline(
    appointment_id: UUID,
    business_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> AppointmentTimelineOut:
    await require_business_api_key(db, business_id, authorization)

    row = (
        await db.execute(
            _joined_appointments_stmt(business_id).where(Appointment.id == appointment_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    appointment, customer, service, staff = row

    # An appointment's full history can span more than one ProcessingRun:
    # the run that originally booked or rebooked it
    # (resulting_appointment_id), a later reschedule/cancel run
    # (target_appointment_id), and -- if it became a no-show -- the
    # recovery run opened for it (recovery_of_appointment_id).
    run_ids = (
        (
            await db.execute(
                select(ProcessingRun.id).where(
                    (ProcessingRun.resulting_appointment_id == appointment_id)
                    | (ProcessingRun.target_appointment_id == appointment_id)
                    | (ProcessingRun.recovery_of_appointment_id == appointment_id)
                )
            )
        )
        .scalars()
        .all()
    )

    entries: list[TimelineEntryOut] = []

    if run_ids:
        steps = (
            (
                await db.execute(
                    select(WorkflowStep).where(WorkflowStep.processing_run_id.in_(run_ids))
                )
            )
            .scalars()
            .all()
        )
        entries.extend(
            TimelineEntryOut(
                at=s.created_at,
                label=_STEP_LABELS.get(s.step_name, s.step_name),
                detail=json.dumps(s.detail) if s.detail else None,
                source="workflow_step",
            )
            for s in steps
        )

    audit_events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "Appointment",
                    AuditEvent.entity_id == appointment_id,
                )
            )
        )
        .scalars()
        .all()
    )
    entries.extend(
        TimelineEntryOut(
            at=a.created_at, label=_audit_label(a), detail=a.to_state, source="audit_event"
        )
        for a in audit_events
    )

    notifications = (
        (
            await db.execute(
                select(Notification).where(Notification.appointment_id == appointment_id)
            )
        )
        .scalars()
        .all()
    )
    entries.extend(
        TimelineEntryOut(
            at=n.created_at,
            label=f"{n.subject} → {n.status.value}",
            detail=f"channel={n.channel}",
            source="notification",
        )
        for n in notifications
    )

    jobs = (
        (
            await db.execute(
                select(ScheduledJob).where(
                    ScheduledJob.payload["appointment_id"].astext == str(appointment_id)
                )
            )
        )
        .scalars()
        .all()
    )
    entries.extend(
        TimelineEntryOut(
            at=j.created_at,
            label=f"Reminder job scheduled for {j.run_at.isoformat()}",
            detail=f"status={j.status.value}",
            source="scheduled_job",
        )
        for j in jobs
    )

    entries.sort(key=lambda e: e.at)

    return AppointmentTimelineOut(
        appointment=_appointment_out(appointment, customer, service, staff), entries=entries
    )


@router.get("/customers", response_model=list[CustomerListItemOut])
async def list_customers(
    business_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> list[CustomerListItemOut]:
    await require_business_api_key(db, business_id, authorization)
    customers = (
        (
            await db.execute(
                select(Customer).where(Customer.business_id == business_id).order_by(Customer.name)
            )
        )
        .scalars()
        .all()
    )

    now = datetime.now(UTC)
    result: list[CustomerListItemOut] = []
    # One query per customer -- simple and readable, and fine at the
    # data volumes this dashboard is built for (a demo/portfolio
    # business, not a high-traffic multi-tenant deployment).
    for customer in customers:
        appointments = (
            (
                await db.execute(
                    select(Appointment)
                    .where(Appointment.customer_id == customer.id)
                    .order_by(Appointment.start_at.desc())
                )
            )
            .scalars()
            .all()
        )
        upcoming = next(
            (
                a
                for a in sorted(appointments, key=lambda a: a.start_at)
                if a.status == AppointmentStatus.BOOKED and a.start_at > now
            ),
            None,
        )
        latest = appointments[0] if appointments else None
        result.append(
            CustomerListItemOut(
                id=customer.id,
                name=customer.name,
                contact=customer.contact,
                total_appointments=len(appointments),
                upcoming_appointment_at=upcoming.start_at if upcoming else None,
                latest_status=latest.status.value if latest else None,
            )
        )
    return result


@router.get("/notifications", response_model=list[NotificationListItemOut])
async def list_notifications(
    business_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> list[NotificationListItemOut]:
    await require_business_api_key(db, business_id, authorization)
    stmt = (
        select(Notification, Customer.name)
        .join(Appointment, Appointment.id == Notification.appointment_id)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(Appointment.business_id == business_id)
        .order_by(Notification.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        NotificationListItemOut(
            id=n.id,
            created_at=n.created_at,
            customer_name=name,
            appointment_id=n.appointment_id,
            subject=n.subject,
            channel=n.channel,
            status=n.status.value,
        )
        for n, name in rows
    ]


@router.get("/jobs", response_model=list[ScheduledJobListItemOut])
async def list_jobs(
    business_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> list[ScheduledJobListItemOut]:
    """Only SEND_REMINDER is a real, per-entity ScheduledJob row -- the
    no-show sweep and expired-run cleanup are periodic sweeps with no job
    row of their own (see app.workflow.jobs), so they show up in an
    appointment's timeline (via the AuditEvent the sweep writes) but not
    here."""
    await require_business_api_key(db, business_id, authorization)
    # ScheduledJob has no business_id column (it's keyed by an
    # appointment_id inside its JSONB payload) -- filtered in Python
    # rather than a JSONB-cast SQL join, for the same simplicity/scale
    # reasoning as list_customers above.
    jobs = (
        (await db.execute(select(ScheduledJob).order_by(ScheduledJob.run_at.desc())))
        .scalars()
        .all()
    )

    result: list[ScheduledJobListItemOut] = []
    for job in jobs:
        appointment_id_str = job.payload.get("appointment_id")
        if not appointment_id_str:
            continue
        appointment = await db.get(Appointment, UUID(appointment_id_str))
        if appointment is None or appointment.business_id != business_id:
            continue
        customer = await db.get(Customer, appointment.customer_id)
        result.append(
            ScheduledJobListItemOut(
                id=job.id,
                job_type=job.job_type.value,
                status=job.status.value,
                run_at=job.run_at,
                attempts=job.attempts,
                last_error=job.last_error,
                customer_name=customer.name if customer else None,
                appointment_id=appointment.id,
            )
        )
    return result


@router.get("/overview", response_model=OverviewSummaryOut)
async def get_overview(
    business_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> OverviewSummaryOut:
    await require_business_api_key(db, business_id, authorization)
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    upcoming_appointments = await db.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.business_id == business_id,
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.start_at > now,
        )
    )
    todays_appointments = await db.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.business_id == business_id,
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.start_at >= today_start,
            Appointment.start_at < today_end,
        )
    )
    no_shows = await db.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.business_id == business_id,
            Appointment.status == AppointmentStatus.NO_SHOW,
        )
    )
    open_escalations = await db.scalar(
        select(func.count())
        .select_from(EscalationCase)
        .join(ProcessingRun, ProcessingRun.id == EscalationCase.processing_run_id)
        .where(
            ProcessingRun.business_id == business_id,
            EscalationCase.status == EscalationStatus.OPEN,
        )
    )

    business_appointment_ids = select(cast(Appointment.id, String)).where(
        Appointment.business_id == business_id
    )
    pending_reminders = await db.scalar(
        select(func.count())
        .select_from(ScheduledJob)
        .where(
            ScheduledJob.job_type == ScheduledJobType.SEND_REMINDER,
            ScheduledJob.status == ScheduledJobStatus.PENDING,
            ScheduledJob.payload["appointment_id"].astext.in_(business_appointment_ids),
        )
    )

    notifications_sent = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .join(Appointment, Appointment.id == Notification.appointment_id)
        .where(
            Appointment.business_id == business_id, Notification.status == NotificationStatus.SENT
        )
    )
    notifications_failed = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .join(Appointment, Appointment.id == Notification.appointment_id)
        .where(
            Appointment.business_id == business_id, Notification.status == NotificationStatus.FAILED
        )
    )

    return OverviewSummaryOut(
        upcoming_appointments=upcoming_appointments or 0,
        todays_appointments=todays_appointments or 0,
        pending_reminders=pending_reminders or 0,
        no_shows=no_shows or 0,
        open_escalations=open_escalations or 0,
        notifications_sent=notifications_sent or 0,
        notifications_failed=notifications_failed or 0,
    )


@router.get("/activity", response_model=list[ActivityEntryOut])
async def list_recent_activity(
    business_id: UUID,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> list[ActivityEntryOut]:
    """A business-wide "what has the automation been doing" feed for the
    Overview page -- the per-appointment timeline above answers "what
    happened to this appointment"; this answers "what's happened lately,
    across every appointment." Same three sources (AuditEvent,
    Notification, EscalationCase), just scoped to the whole business and
    capped rather than tied to one appointment_id."""
    await require_business_api_key(db, business_id, authorization)

    entries: list[ActivityEntryOut] = []

    audit_rows = (
        await db.execute(
            select(AuditEvent, Customer.name)
            .join(Appointment, Appointment.id == AuditEvent.entity_id)
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(AuditEvent.entity_type == "Appointment", Appointment.business_id == business_id)
            .order_by(AuditEvent.created_at.desc())
            .limit(limit)
        )
    ).all()
    entries.extend(
        ActivityEntryOut(
            at=a.created_at,
            label=_audit_label(a),
            detail=a.to_state,
            source="audit_event",
            customer_name=name,
            appointment_id=a.entity_id,
        )
        for a, name in audit_rows
    )

    notification_rows = (
        await db.execute(
            select(Notification, Customer.name)
            .join(Appointment, Appointment.id == Notification.appointment_id)
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(Appointment.business_id == business_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
    ).all()
    entries.extend(
        ActivityEntryOut(
            at=n.created_at,
            label=f"{n.subject} → {n.status.value}",
            detail=f"channel={n.channel}",
            source="notification",
            customer_name=name,
            appointment_id=n.appointment_id,
        )
        for n, name in notification_rows
    )

    escalation_rows = (
        await db.execute(
            select(EscalationCase, Customer.name)
            .join(ProcessingRun, ProcessingRun.id == EscalationCase.processing_run_id)
            .join(Customer, Customer.id == ProcessingRun.customer_id)
            .where(ProcessingRun.business_id == business_id)
            .order_by(EscalationCase.created_at.desc())
            .limit(limit)
        )
    ).all()
    for e, name in escalation_rows:
        entries.append(
            ActivityEntryOut(
                at=e.created_at,
                label=f"Escalated: {e.reason}",
                detail=None,
                source="escalation",
                customer_name=name,
                appointment_id=None,
            )
        )
        if e.resolved_at is not None:
            entries.append(
                ActivityEntryOut(
                    at=e.resolved_at,
                    label="Escalation resolved",
                    detail=e.reason,
                    source="escalation",
                    customer_name=name,
                    appointment_id=None,
                )
            )

    entries.sort(key=lambda entry: entry.at, reverse=True)
    return entries[:limit]
