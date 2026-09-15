"""Resend: the one real (non-mock, non-console) NotificationService
implementation, and the only module in this codebase that talks to it --
matching how app/ai/providers/claude.py is the only module that imports
the Anthropic SDK. Email only; Customer.contact has no channel marker, so
a contact that isn't a deliverable email address is simply a send that
fails -- caught here and recorded as NotificationStatus.FAILED, never
raised. Nothing upstream (orchestrator.py, jobs.py) needs to know a real
vendor is involved at all: same Protocol, same call sites, as Mock and
Console.
"""

from __future__ import annotations

import logging
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Appointment, Customer
from app.workflow.models import Notification, NotificationStatus, ProcessingRun
from app.workflow.notifications import (
    _cancellation_content,
    _confirmation_content,
    _no_show_recovery_content,
    _reminder_content,
    _reschedule_confirmation_content,
)

logger = logging.getLogger("notifications.resend")

_RESEND_API_URL = "https://api.resend.com/emails"


class ResendNotificationProvider:
    def __init__(
        self,
        *,
        api_key: str,
        from_email: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._from_email = from_email
        # None -> httpx's real network transport. Tests inject an
        # httpx.MockTransport instead, so the failure-handling path below
        # (the actual thing Phase 8 needs verified) can be exercised
        # without ever making a real network call.
        self._transport = transport

    async def send_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _confirmation_content(appointment)
        return await self._send(db, run.id, appointment, customer, subject, body)

    async def send_reminder(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
    ) -> Notification:
        subject, body = _reminder_content(appointment)
        return await self._send(db, None, appointment, customer, subject, body)

    async def send_no_show_recovery(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _no_show_recovery_content(appointment)
        return await self._send(db, run.id, appointment, customer, subject, body)

    async def send_cancellation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _cancellation_content(appointment)
        return await self._send(db, run.id, appointment, customer, subject, body)

    async def send_reschedule_confirmation(
        self,
        db: AsyncSession,
        *,
        appointment: Appointment,
        customer: Customer,
        run: ProcessingRun,
    ) -> Notification:
        subject, body = _reschedule_confirmation_content(appointment)
        return await self._send(db, run.id, appointment, customer, subject, body)

    async def _send(
        self,
        db: AsyncSession,
        processing_run_id: UUID | None,
        appointment: Appointment,
        customer: Customer,
        subject: str,
        body: str,
    ) -> Notification:
        status = NotificationStatus.SENT
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                response = await client.post(
                    _RESEND_API_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "from": self._from_email,
                        "to": [customer.contact],
                        "subject": subject,
                        "text": body,
                    },
                    timeout=10.0,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            # Never a reason to fail the appointment action that triggered
            # this send -- the caller already committed that in Postgres.
            # Recorded here, same as a calendar sync failure, for the
            # audit trail and (eventually) a manual retry to pick up.
            logger.warning("send to %s failed: %s", customer.contact, exc)
            status = NotificationStatus.FAILED

        notification = Notification(
            processing_run_id=processing_run_id,
            appointment_id=appointment.id,
            channel="email",
            to_contact=customer.contact,
            subject=subject,
            body=body,
            status=status,
        )
        db.add(notification)
        await db.flush()
        return notification
