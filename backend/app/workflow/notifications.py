"""The notification boundary: workflow code depends on NotificationService,
never on a specific email/SMS vendor. Three implementations exist: this
module holds the Protocol, MockNotificationProvider (silent, for tests),
and ConsoleNotificationProvider (logs instead of a real send, no
credentials, the local-dev default). ResendNotificationProvider (Phase 8's
real vendor) lives in notifications_resend.py -- the one module that talks
to Resend, matching how app/ai/providers/claude.py is the only module
that imports the Anthropic SDK.
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Appointment, Customer
from app.workflow.models import Notification, NotificationStatus, ProcessingRun

logger = logging.getLogger("notifications.console")


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


# Message content is composed once here and reused by every provider that
# doesn't need vendor-specific formatting (Mock, Console, and Resend's
# plain-text body) -- one place to change the wording, not five per
# provider.


def _confirmation_content(appointment: Appointment) -> tuple[str, str]:
    return (
        "Appointment confirmed",
        f"Your appointment is confirmed for {appointment.start_at.isoformat()} (UTC).",
    )


def _reminder_content(appointment: Appointment) -> tuple[str, str]:
    return (
        "Appointment reminder",
        f"Reminder: your appointment is at {appointment.start_at.isoformat()} (UTC).",
    )


def _no_show_recovery_content(appointment: Appointment) -> tuple[str, str]:
    return (
        "We missed you",
        f"Sorry we missed you for your {appointment.start_at.isoformat()} (UTC) "
        "appointment -- would you like to reschedule?",
    )


def _cancellation_content(appointment: Appointment) -> tuple[str, str]:
    return (
        "Appointment cancelled",
        f"Your appointment for {appointment.start_at.isoformat()} (UTC) has been cancelled.",
    )


def _reschedule_confirmation_content(appointment: Appointment) -> tuple[str, str]:
    return (
        "Appointment rescheduled",
        f"Your appointment has been moved to {appointment.start_at.isoformat()} (UTC).",
    )


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
        subject, body = _confirmation_content(appointment)
        return await self._record(db, run.id, appointment, customer, subject, body)

    async def send_reminder(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
    ) -> Notification:
        subject, body = _reminder_content(appointment)
        return await self._record(db, None, appointment, customer, subject, body)

    async def send_no_show_recovery(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _no_show_recovery_content(appointment)
        return await self._record(db, run.id, appointment, customer, subject, body)

    async def send_cancellation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _cancellation_content(appointment)
        return await self._record(db, run.id, appointment, customer, subject, body)

    async def send_reschedule_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _reschedule_confirmation_content(appointment)
        return await self._record(db, run.id, appointment, customer, subject, body)

    async def _record(
        self,
        db: AsyncSession,
        processing_run_id: UUID | None,
        appointment: Appointment,
        customer: Customer,
        subject: str,
        body: str,
    ) -> Notification:
        notification = Notification(
            processing_run_id=processing_run_id,
            appointment_id=appointment.id,
            channel="mock",
            to_contact=customer.contact,
            subject=subject,
            body=body,
            status=NotificationStatus.SENT,
        )
        db.add(notification)
        await db.flush()
        return notification


class ConsoleNotificationProvider:
    """The credentials-free local-dev default (NOTIFICATION_PROVIDER=
    console): logs what would have been sent and persists the same
    Notification row a real provider would, but never contacts a real
    vendor. Always succeeds -- there's no network call that could fail."""

    async def send_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _confirmation_content(appointment)
        return await self._record(db, run.id, appointment, customer, subject, body)

    async def send_reminder(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
    ) -> Notification:
        subject, body = _reminder_content(appointment)
        return await self._record(db, None, appointment, customer, subject, body)

    async def send_no_show_recovery(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _no_show_recovery_content(appointment)
        return await self._record(db, run.id, appointment, customer, subject, body)

    async def send_cancellation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _cancellation_content(appointment)
        return await self._record(db, run.id, appointment, customer, subject, body)

    async def send_reschedule_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _reschedule_confirmation_content(appointment)
        return await self._record(db, run.id, appointment, customer, subject, body)

    async def _record(
        self,
        db: AsyncSession,
        processing_run_id: UUID | None,
        appointment: Appointment,
        customer: Customer,
        subject: str,
        body: str,
    ) -> Notification:
        logger.info("to=%s subject=%r body=%r", customer.contact, subject, body)
        notification = Notification(
            processing_run_id=processing_run_id,
            appointment_id=appointment.id,
            channel="console",
            to_contact=customer.contact,
            subject=subject,
            body=body,
            status=NotificationStatus.SENT,
        )
        db.add(notification)
        await db.flush()
        return notification
