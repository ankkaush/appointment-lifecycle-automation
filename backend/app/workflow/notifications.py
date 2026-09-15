"""The notification boundary: workflow code depends on NotificationService,
never on a specific email/SMS vendor. MockProvider is the only
implementation until Phase 8 introduces a real one (e.g. Resend) -- that
phase adds a new module here, not new call sites.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Appointment, Customer
from app.workflow.models import Notification, NotificationStatus, ProcessingRun


class NotificationService(Protocol):
    async def send_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification: ...

    async def send_reminder(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
    ) -> Notification:
        """No `run` parameter -- a reminder fires long after its
        ProcessingRun reached SUCCEEDED, from a background sweep with no
        request context at all."""
        ...

    async def send_no_show_recovery(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        """The recovery outreach message -- `run` is the new kind=RECOVERY
        ProcessingRun this outreach opens, not the original booking's."""
        ...

    async def send_cancellation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification: ...

    async def send_reschedule_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        """`appointment` is the same row as before, at its new start_at --
        rescheduling moves it in place rather than creating a new one."""
        ...


class MockNotificationProvider:
    """Persists a Notification row without contacting any real provider --
    the customer-visible side effect of "confirmation sent" is real (it's
    in the audit trail), the delivery is not."""

    async def send_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        notification = Notification(
            processing_run_id=run.id,
            appointment_id=appointment.id,
            channel="mock",
            to_contact=customer.contact,
            subject="Appointment confirmed",
            body=(f"Your appointment is confirmed for {appointment.start_at.isoformat()} (UTC)."),
            status=NotificationStatus.SENT,
        )
        db.add(notification)
        await db.flush()
        return notification

    async def send_reminder(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
    ) -> Notification:
        notification = Notification(
            processing_run_id=None,
            appointment_id=appointment.id,
            channel="mock",
            to_contact=customer.contact,
            subject="Appointment reminder",
            body=(f"Reminder: your appointment is at {appointment.start_at.isoformat()} (UTC)."),
            status=NotificationStatus.SENT,
        )
        db.add(notification)
        await db.flush()
        return notification

    async def send_no_show_recovery(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        notification = Notification(
            processing_run_id=run.id,
            appointment_id=appointment.id,
            channel="mock",
            to_contact=customer.contact,
            subject="We missed you",
            body=(
                f"Sorry we missed you for your {appointment.start_at.isoformat()} (UTC) "
                "appointment -- would you like to reschedule?"
            ),
            status=NotificationStatus.SENT,
        )
        db.add(notification)
        await db.flush()
        return notification

    async def send_cancellation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        notification = Notification(
            processing_run_id=run.id,
            appointment_id=appointment.id,
            channel="mock",
            to_contact=customer.contact,
            subject="Appointment cancelled",
            body=(
                f"Your appointment for {appointment.start_at.isoformat()} (UTC) has been "
                "cancelled."
            ),
            status=NotificationStatus.SENT,
        )
        db.add(notification)
        await db.flush()
        return notification

    async def send_reschedule_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        notification = Notification(
            processing_run_id=run.id,
            appointment_id=appointment.id,
            channel="mock",
            to_contact=customer.contact,
            subject="Appointment rescheduled",
            body=(f"Your appointment has been moved to {appointment.start_at.isoformat()} (UTC)."),
            status=NotificationStatus.SENT,
        )
        db.add(notification)
        await db.flush()
        return notification
