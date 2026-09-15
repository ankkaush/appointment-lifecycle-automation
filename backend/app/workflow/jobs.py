"""The worker-facing side of background processing: recurring sweeps
(no-show detection, expiring abandoned runs) plus the one-off
ScheduledJob consumer (reminders). Depends on orchestrator.py (for
start_recovery) -- one-directional; orchestrator.py never imports this
module, so there's no cycle.

Nothing here is exposed over HTTP except through the manual admin trigger
in app/api/routes_jobs.py. The real driver is app/worker.py's loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Appointment, AppointmentStatus, Business, Customer
from app.domain.noshow import is_no_show
from app.workflow import orchestrator
from app.workflow.models import (
    AuditEvent,
    ProcessingRun,
    ProcessingRunState,
    ScheduledJob,
    ScheduledJobStatus,
    ScheduledJobType,
    WorkflowStep,
)
from app.workflow.notifications import NotificationService

# How long an AWAITING_CLARIFICATION / AWAITING_CONFIRMATION run may sit
# idle (measured off updated_at -- no new column needed) before the
# expiry sweep gives up on it. One constant for both: a customer who
# never answers a clarifying question, never picks an offered slot, or
# never replies to a recovery outreach is the same shape of "abandoned,"
# and none of it is configured per-business -- proportional to what this
# phase actually needs, not a knob nobody asked for yet.
AWAITING_REPLY_TIMEOUT_MINUTES = 60

_EXPIRABLE_STATES = (
    ProcessingRunState.AWAITING_CLARIFICATION,
    ProcessingRunState.AWAITING_CONFIRMATION,
)


async def run_due_reminders(
    db: AsyncSession, *, notification_service: NotificationService, now: datetime | None = None
) -> int:
    """Claims due SEND_REMINDER jobs with SKIP LOCKED (safe under more
    than one worker process) and sends each. Re-checks the appointment is
    still BOOKED before sending -- a cancelled appointment's reminder job
    is a no-op, not an error."""
    now = now or datetime.now(UTC)

    due_ids = (
        (
            await db.execute(
                select(ScheduledJob.id)
                .where(
                    ScheduledJob.job_type == ScheduledJobType.SEND_REMINDER,
                    ScheduledJob.status == ScheduledJobStatus.PENDING,
                    ScheduledJob.run_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    processed = 0
    for job_id in due_ids:
        job = await db.get(ScheduledJob, job_id)
        job.status = ScheduledJobStatus.LOCKED
        await db.flush()

        try:
            appointment_id = job.payload["appointment_id"]
            appointment = await db.get(Appointment, appointment_id)
            if appointment is not None and appointment.status == AppointmentStatus.BOOKED:
                customer = await db.get(Customer, appointment.customer_id)
                await notification_service.send_reminder(
                    db, appointment=appointment, customer=customer
                )
            job.status = ScheduledJobStatus.DONE
        except Exception as exc:  # noqa: BLE001 -- a failing job must never crash the sweep
            job.attempts += 1
            job.status = ScheduledJobStatus.FAILED_RETRYABLE
            job.last_error = str(exc)

        await db.flush()
        processed += 1

    await db.commit()
    return processed


async def sweep_no_shows(
    db: AsyncSession, *, notification_service: NotificationService, now: datetime | None = None
) -> int:
    """Marks overdue BOOKED appointments NO_SHOW (the deterministic rule
    in app.domain.noshow) and immediately opens a recovery attempt for
    each -- the two always happen together, per the original example
    this whole feature was designed around."""
    now = now or datetime.now(UTC)

    candidates = (
        (
            await db.execute(
                select(Appointment).where(Appointment.status == AppointmentStatus.BOOKED)
            )
        )
        .scalars()
        .all()
    )

    processed = 0
    for appointment in candidates:
        business = await db.get(Business, appointment.business_id)
        if not is_no_show(appointment, business, now):
            continue

        appointment.status = AppointmentStatus.NO_SHOW
        db.add(
            AuditEvent(
                entity_type="Appointment",
                entity_id=appointment.id,
                from_state=AppointmentStatus.BOOKED.value,
                to_state=AppointmentStatus.NO_SHOW.value,
                reason="no-show sweep: past end time + grace period",
            )
        )
        await db.flush()

        await orchestrator.start_recovery(
            db, appointment.id, notification_service=notification_service, now=now
        )
        processed += 1

    return processed


async def sweep_expired_runs(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Abandoned conversations -- a clarifying question, an offered slot,
    or a recovery outreach nobody ever answered -- close as unresolved
    rather than sitting open forever."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=AWAITING_REPLY_TIMEOUT_MINUTES)

    stale_runs = (
        (
            await db.execute(
                select(ProcessingRun).where(
                    ProcessingRun.state.in_(_EXPIRABLE_STATES),
                    ProcessingRun.updated_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    for run in stale_runs:
        db.add(
            AuditEvent(
                processing_run_id=run.id,
                entity_type="ProcessingRun",
                entity_id=run.id,
                from_state=run.state.value,
                to_state=ProcessingRunState.EXPIRED.value,
                reason=f"no reply within {AWAITING_REPLY_TIMEOUT_MINUTES} minutes",
            )
        )
        run.state = ProcessingRunState.EXPIRED
        db.add(WorkflowStep(processing_run_id=run.id, step_name="expired", detail=None))

    await db.commit()
    return len(stale_runs)


async def run_worker_tick(
    db: AsyncSession, *, notification_service: NotificationService, now: datetime | None = None
) -> dict[str, int]:
    """One pass of everything this phase owns -- called by the worker
    loop on a timer, and by the manual admin trigger for on-demand runs."""
    now = now or datetime.now(UTC)
    reminders_sent = await run_due_reminders(db, notification_service=notification_service, now=now)
    no_shows_detected = await sweep_no_shows(db, notification_service=notification_service, now=now)
    runs_expired = await sweep_expired_runs(db, now=now)
    return {
        "reminders_sent": reminders_sent,
        "no_shows_detected": no_shows_detected,
        "runs_expired": runs_expired,
    }
