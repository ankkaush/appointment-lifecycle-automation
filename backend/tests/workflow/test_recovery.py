"""No-show recovery: the customer's reply reuses the exact same
AWAITING_CLARIFICATION / reply_to_clarification machinery a clarifying
question uses. These tests prove that reuse actually works end to end,
including the RESCHEDULE-intent widening that's scoped to recovery runs
only. No live API calls.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.fake import FakeInterpreter
from app.ai.schemas import AIInvocationMetadata, Intent, InterpretationOutcome, InterpretedRequest
from app.domain.exceptions import DomainError
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Service,
    StaffResource,
)
from app.workflow import orchestrator
from app.workflow.calendar import MockCalendarProvider
from app.workflow.jobs import sweep_no_shows
from app.workflow.models import ProcessingRun, ProcessingRunKind, ProcessingRunState
from app.workflow.notifications import MockNotificationProvider
from app.workflow.schemas import ConfirmSlotIn, StartRequestIn


@dataclass
class _SequencedStubInterpreter:
    outcomes: list[InterpretedRequest]
    calls: int = field(default=0, init=False)

    async def interpret(self, message, *, today, known_services):
        req = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        metadata = AIInvocationMetadata(
            provider="stub", model="stub-v1", latency_ms=0.0, input_tokens=0, output_tokens=0
        )
        return InterpretationOutcome(request=req, metadata=metadata)


async def _book_appointment_only(
    db: AsyncSession, business: Business, customer: Customer
) -> Appointment:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Book me a Haircut please"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    chosen = run.offered_slots[0]
    confirm_payload = ConfirmSlotIn(
        start_at=datetime.fromisoformat(chosen["start_at"]), idempotency_key=str(uuid4())
    )
    result = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    return await db.get(Appointment, result.resulting_appointment_id)


async def _book_and_mark_no_show(
    db: AsyncSession, business: Business, customer: Customer
) -> Appointment:
    appointment = await _book_appointment_only(db, business, customer)
    past_grace = appointment.end_at + timedelta(minutes=business.no_show_grace_period_minutes + 1)
    await sweep_no_shows(db, notification_service=MockNotificationProvider(), now=past_grace)
    await db.refresh(appointment)
    return appointment


@pytest.mark.asyncio
async def test_start_recovery_pre_fills_service_and_sends_outreach(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book_and_mark_no_show(db, business, customer)

    recovery_run = (
        await db.execute(
            select(ProcessingRun).where(ProcessingRun.recovery_of_appointment_id == appointment.id)
        )
    ).scalar_one()

    assert recovery_run.kind == ProcessingRunKind.RECOVERY
    assert recovery_run.state == ProcessingRunState.AWAITING_CLARIFICATION
    assert recovery_run.matched_service_id == service.id
    assert "missed you" in recovery_run.messages[-1]["content"].lower()


@pytest.mark.asyncio
async def test_recovery_reply_resolves_and_rebooks(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    original = await _book_and_mark_no_show(db, business, customer)
    recovery_run = (
        await db.execute(
            select(ProcessingRun).where(ProcessingRun.recovery_of_appointment_id == original.id)
        )
    ).scalar_one()

    # Deliberately no service_hint on this outcome -- proves the reply
    # resolves using the pre-filled service, not a fresh match against
    # text the customer's reply never repeats.
    stub = _SequencedStubInterpreter(
        [InterpretedRequest(intent=Intent.RESCHEDULE, is_ambiguous=False, confidence=0.9)]
    )
    replied = await orchestrator.reply_to_clarification(
        db,
        recovery_run.id,
        "Can I come Thursday afternoon instead?",
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert replied.state == ProcessingRunState.AWAITING_CONFIRMATION
    assert replied.offered_slots

    chosen = replied.offered_slots[0]
    confirm_payload = ConfirmSlotIn(
        start_at=datetime.fromisoformat(chosen["start_at"]), idempotency_key=str(uuid4())
    )
    confirmed = await orchestrator.confirm_slot(
        db,
        replied.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert confirmed.state == ProcessingRunState.SUCCEEDED
    new_appointment = await db.get(Appointment, confirmed.resulting_appointment_id)
    assert new_appointment.rebooked_from_id == original.id
    assert new_appointment.status == AppointmentStatus.BOOKED

    await db.refresh(original)
    assert original.status == AppointmentStatus.NO_SHOW  # untouched, immutable history


@pytest.mark.asyncio
async def test_reschedule_intent_accepted_for_recovery_but_escalates_for_ordinary_runs(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    # The RESCHEDULE-acceptance widening is scoped to kind=RECOVERY only
    # -- an ordinary customer-initiated run still escalates on it, since
    # cancel/reschedule policy for an existing appointment isn't
    # automated yet.
    stub = _SequencedStubInterpreter(
        [InterpretedRequest(intent=Intent.RESCHEDULE, is_ambiguous=False, confidence=0.9)]
    )
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Can I move my appointment?"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    assert run.state == ProcessingRunState.ESCALATED


@pytest.mark.asyncio
async def test_recovery_reply_still_ambiguous_can_be_clarified(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    original = await _book_and_mark_no_show(db, business, customer)
    recovery_run = (
        await db.execute(
            select(ProcessingRun).where(ProcessingRun.recovery_of_appointment_id == original.id)
        )
    ).scalar_one()

    stub = _SequencedStubInterpreter(
        [
            InterpretedRequest(
                intent=Intent.BOOK,
                is_ambiguous=True,
                ambiguity_reason="no date given",
                confidence=0.3,
            ),
            InterpretedRequest(intent=Intent.BOOK, is_ambiguous=False, confidence=0.9),
        ]
    )
    first = await orchestrator.reply_to_clarification(
        db,
        recovery_run.id,
        "yes I'd like to",
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    assert first.state == ProcessingRunState.AWAITING_CLARIFICATION
    assert first.clarification_rounds == 1

    second = await orchestrator.reply_to_clarification(
        db,
        recovery_run.id,
        "Thursday afternoon",
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    assert second.state == ProcessingRunState.AWAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_start_recovery_rejects_non_no_show_appointment(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _book_appointment_only(db, business, customer)  # still BOOKED
    with pytest.raises(DomainError):
        await orchestrator.start_recovery(
            db, appointment.id, notification_service=MockNotificationProvider()
        )
