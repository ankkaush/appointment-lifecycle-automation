"""The bounded clarification loop: ambiguous -> one clarifying question ->
reply resolves it, or repeated ambiguity escalates after the cap. No live
API calls -- a small sequenced stub plays the model's role across turns.
"""

from dataclasses import dataclass, field

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.schemas import AIInvocationMetadata, Intent, InterpretationOutcome, InterpretedRequest
from app.domain.models import Business, Customer, Service, StaffResource
from app.workflow import orchestrator
from app.workflow.calendar import MockCalendarProvider
from app.workflow.exceptions import ProcessingRunStateError
from app.workflow.models import EscalationCase, ProcessingRunState
from app.workflow.notifications import MockNotificationProvider
from app.workflow.schemas import StartRequestIn

# The internal diagnostic reason -- never shown to the customer directly
# (architecture review, Finding 2). What the customer actually sees comes
# from app.workflow.clarify.derive_clarifying_question instead.
AMBIGUITY_REASON = "No date or time preference provided."
DATE_CLARIFYING_QUESTION = "What day would you like to come in?"


@dataclass
class _SequencedStubInterpreter:
    """Returns each outcome in order across successive calls, holding on
    the last one if called more times than provided -- lets one stub play
    the model's role across multiple turns of the same conversation."""

    outcomes: list[InterpretedRequest]
    calls: int = field(default=0, init=False)

    async def interpret(self, message, *, today, known_services):
        req = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        metadata = AIInvocationMetadata(
            provider="stub", model="stub-v1", latency_ms=0.0, input_tokens=0, output_tokens=0
        )
        return InterpretationOutcome(request=req, metadata=metadata)


def _ambiguous() -> InterpretedRequest:
    # intent=book, no date, no service -- derive_clarifying_question asks
    # about the date first (see test_clarify.py for the priority rule).
    return InterpretedRequest(
        intent=Intent.BOOK, is_ambiguous=True, ambiguity_reason=AMBIGUITY_REASON, confidence=0.4
    )


def _ambiguous_intent(candidates: list[Intent]) -> InterpretedRequest:
    return InterpretedRequest(
        intent=candidates[0],
        is_ambiguous=True,
        ambiguity_reason="Could be a booking request or a question.",
        candidate_intents=candidates,
        confidence=0.4,
    )


def _resolved(service_hint: str = "Haircut") -> InterpretedRequest:
    return InterpretedRequest(
        intent=Intent.BOOK, service_hint=service_hint, is_ambiguous=False, confidence=0.9
    )


@pytest.mark.asyncio
async def test_ambiguous_request_asks_one_clarifying_question(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    stub = _SequencedStubInterpreter([_ambiguous()])
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Can I come Friday?"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.AWAITING_CLARIFICATION
    assert run.clarification_rounds == 1
    assert run.messages[-1]["role"] == "assistant"
    # The customer sees the deterministically-derived question, not the
    # AI's internal ambiguity_reason -- see Finding 2.
    assert run.messages[-1]["content"] == DATE_CLARIFYING_QUESTION
    assert run.messages[-1]["content"] != AMBIGUITY_REASON


@pytest.mark.asyncio
async def test_reply_resolves_ambiguity_and_offers_slots(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    stub = _SequencedStubInterpreter([_ambiguous(), _resolved("Haircut")])
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Can I come Friday?"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    assert run.state == ProcessingRunState.AWAITING_CLARIFICATION

    result = await orchestrator.reply_to_clarification(
        db,
        run.id,
        "I want to book a Haircut",
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert result.state == ProcessingRunState.AWAITING_CONFIRMATION
    assert result.offered_slots
    # customer -> assistant question -> customer reply
    assert [m["role"] for m in result.messages] == ["customer", "assistant", "customer"]


@pytest.mark.asyncio
async def test_still_ambiguous_after_cap_escalates(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    stub = _SequencedStubInterpreter([_ambiguous(), _ambiguous(), _ambiguous()])
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Can I come Friday?"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    assert run.state == ProcessingRunState.AWAITING_CLARIFICATION
    assert run.clarification_rounds == 1

    run = await orchestrator.reply_to_clarification(
        db,
        run.id,
        "still not sure",
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    assert run.state == ProcessingRunState.AWAITING_CLARIFICATION
    assert run.clarification_rounds == 2

    run = await orchestrator.reply_to_clarification(
        db,
        run.id,
        "still not sure",
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )
    assert run.state == ProcessingRunState.ESCALATED

    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert "2 clarification rounds" in case.reason


@pytest.mark.asyncio
async def test_reply_rejected_when_run_not_awaiting_clarification(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    from app.ai.providers.fake import FakeInterpreter

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
    assert run.state == ProcessingRunState.AWAITING_CONFIRMATION

    with pytest.raises(ProcessingRunStateError):
        await orchestrator.reply_to_clarification(
            db,
            run.id,
            "anything",
            interpreter=FakeInterpreter(),
            notification_service=MockNotificationProvider(),
            calendar_provider=MockCalendarProvider(),
        )


# --- candidate_intents routing (architecture review, Finding 1) ----------


@pytest.mark.asyncio
async def test_intent_ambiguous_with_book_candidate_asks_disambiguation(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    # "Can I come Friday?" -- intent=book is the model's single best
    # guess, but candidate_intents flags it could equally be a question.
    # Book being a candidate is what routes this to clarification instead
    # of an immediate escalation.
    stub = _SequencedStubInterpreter([_ambiguous_intent([Intent.BOOK, Intent.QUESTION])])
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Can I come Friday?"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.AWAITING_CLARIFICATION
    assert run.clarification_rounds == 1
    assert (
        run.messages[-1]["content"] == "Are you looking to book an appointment, or ask a question?"
    )
    # The internal reason never reached the customer.
    assert run.messages[-1]["content"] != "Could be a booking request or a question."


@pytest.mark.asyncio
async def test_ambiguous_without_book_candidate_still_escalates_immediately(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    # Ambiguous, but book is not among the candidates (and intent itself
    # isn't book) -- this must not enter the clarification loop, which
    # stays scoped to the booking path. Same immediate-escalation
    # behavior as a confident non-BOOK intent.
    stub = _SequencedStubInterpreter([_ambiguous_intent([Intent.CANCEL, Intent.RESCHEDULE])])
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="something about my appointment"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.ESCALATED
    assert run.clarification_rounds == 0
