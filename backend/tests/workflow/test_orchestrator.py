from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.fake import FakeInterpreter
from app.ai.schemas import AIInvocationMetadata, Intent, InterpretationOutcome, InterpretedRequest
from app.domain import booking as domain_booking
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Service,
    StaffResource,
)
from app.domain.schemas import BookingRequest
from app.workflow import orchestrator
from app.workflow.calendar import MockCalendarProvider
from app.workflow.exceptions import InvalidSlotChoiceError
from app.workflow.models import (
    AIInvocation,
    EscalationCase,
    Notification,
    ProcessingRunState,
    WorkflowStep,
)
from app.workflow.notifications import MockNotificationProvider
from app.workflow.schemas import ConfirmSlotIn, StartRequestIn


@dataclass
class _StubInterpreter:
    """A purpose-built fake for scenarios FakeInterpreter's simple keyword
    matching can't reach (e.g. an ambiguous BOOK request) -- no network."""

    request: InterpretedRequest

    async def interpret(self, message, *, today, known_services):
        metadata = AIInvocationMetadata(
            provider="stub", model="stub-v1", latency_ms=0.0, input_tokens=0, output_tokens=0
        )
        return InterpretationOutcome(request=self.request, metadata=metadata)


async def _steps(db: AsyncSession, run_id) -> list[str]:
    rows = (
        (
            await db.execute(
                select(WorkflowStep.step_name)
                .where(WorkflowStep.processing_run_id == run_id)
                .order_by(WorkflowStep.created_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@pytest.mark.asyncio
async def test_start_request_offers_slots_for_clear_booking_message(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    payload = StartRequestIn(
        business_id=business.id,
        customer_id=customer.id,
        message="I'd like to book a Haircut appointment please",
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.AWAITING_CONFIRMATION
    assert run.matched_service_id == service.id
    assert run.offered_slots
    assert len(run.offered_slots) > 0

    steps = await _steps(db, run.id)
    assert steps == ["received", "interpreted", "slots_offered", "awaiting_confirmation"]

    invocation = (
        await db.execute(select(AIInvocation).where(AIInvocation.processing_run_id == run.id))
    ).scalar_one()
    assert invocation.intent == "book"
    assert invocation.provider == "fake"


@pytest.mark.asyncio
async def test_start_request_escalates_on_unrecognized_message(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="asdkfj qwer 1234"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.ESCALATED
    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert case.reason


@pytest.mark.asyncio
async def test_start_request_escalates_non_book_intent(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Can I cancel my appointment?"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=FakeInterpreter(),
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.ESCALATED
    steps = await _steps(db, run.id)
    assert "escalated" in steps


# An ambiguous BOOK request now asks one bounded clarifying question
# rather than escalating immediately -- see tests/workflow/test_clarification.py
# for that behavior (test_ambiguous_request_asks_one_clarifying_question
# and test_still_ambiguous_after_cap_escalates for the eventual escalation
# once the clarification cap is exhausted).


@pytest.mark.asyncio
async def test_start_request_escalates_unmatched_service(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    stub = _StubInterpreter(
        InterpretedRequest(
            intent=Intent.BOOK, service_hint="Manicure", is_ambiguous=False, confidence=0.9
        )
    )
    payload = StartRequestIn(
        business_id=business.id, customer_id=customer.id, message="Can I get a manicure?"
    )
    run = await orchestrator.start_request(
        db,
        payload,
        interpreter=stub,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    assert run.state == ProcessingRunState.ESCALATED
    case = (
        await db.execute(select(EscalationCase).where(EscalationCase.processing_run_id == run.id))
    ).scalar_one()
    assert "Manicure" in case.reason


@pytest.mark.asyncio
async def test_confirm_slot_success_books_and_notifies(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
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

    assert result.state == ProcessingRunState.SUCCEEDED
    assert result.resulting_appointment_id is not None

    notification = (
        await db.execute(select(Notification).where(Notification.processing_run_id == run.id))
    ).scalar_one()
    assert notification.to_contact == customer.contact

    steps = await _steps(db, run.id)
    assert steps[-5:] == [
        "booking_attempted",
        "booking_succeeded",
        "calendar_sync_succeeded",
        "confirmation_sent",
        "reminder_scheduled",
    ]


@pytest.mark.asyncio
async def test_confirm_slot_rejects_choice_not_offered(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
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

    bogus_time = datetime.now(UTC) + timedelta(days=365)
    confirm_payload = ConfirmSlotIn(start_at=bogus_time, idempotency_key=str(uuid4()))

    with pytest.raises(InvalidSlotChoiceError):
        await orchestrator.confirm_slot(
            db,
            run.id,
            confirm_payload,
            notification_service=MockNotificationProvider(),
            calendar_provider=MockCalendarProvider(),
        )


@pytest.mark.asyncio
async def test_confirm_slot_reoffers_when_concurrently_taken(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
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
    original_offer = run.offered_slots[0]
    original_start = datetime.fromisoformat(original_offer["start_at"])

    # Simulate another channel booking that exact slot first, between the
    # offer and the confirm -- exactly the race the architecture baseline
    # calls out (Section C: re-check for real, re-offer, don't just error).
    other_customer = Customer(
        business_id=business.id, name="Other Customer", contact="other@example.com"
    )
    db.add(other_customer)
    await db.flush()
    await domain_booking.book_appointment(
        db,
        BookingRequest(
            idempotency_key=str(uuid4()),
            business_id=business.id,
            service_id=service.id,
            staff_id=staff.id,
            customer_id=other_customer.id,
            start_at=original_start,
        ),
    )

    confirm_payload = ConfirmSlotIn(start_at=original_start, idempotency_key=str(uuid4()))
    result = await orchestrator.confirm_slot(
        db,
        run.id,
        confirm_payload,
        notification_service=MockNotificationProvider(),
        calendar_provider=MockCalendarProvider(),
    )

    # Re-offered, not failed: still awaiting confirmation, with fresh
    # slots that no longer include the one that was just taken.
    assert result.state == ProcessingRunState.AWAITING_CONFIRMATION
    assert result.resulting_appointment_id is None
    new_starts = {o["start_at"] for o in result.offered_slots}
    assert original_offer["start_at"] not in new_starts

    steps = await _steps(db, run.id)
    assert "slot_no_longer_available" in steps

    # And the original request's own appointment is untouched -- exactly
    # one BOOKED appointment exists for that slot, belonging to whoever
    # actually won the race.
    matching = (
        (
            await db.execute(
                select(Appointment).where(
                    Appointment.staff_id == staff.id,
                    Appointment.start_at == original_start,
                    Appointment.status == AppointmentStatus.BOOKED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(matching) == 1
