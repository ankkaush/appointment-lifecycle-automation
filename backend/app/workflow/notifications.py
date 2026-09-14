"""The notification boundary: workflow code depends on NotificationService,
never on a specific email/SMS vendor. MockProvider is the only
implementation until Phase 7 introduces a real one (e.g. Resend) -- that
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
            body=(
                f"Your appointment is confirmed for " f"{appointment.start_at.isoformat()} (UTC)."
            ),
            status=NotificationStatus.SENT,
        )
        db.add(notification)
        await db.flush()
        return notification
