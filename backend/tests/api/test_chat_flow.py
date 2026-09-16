"""HTTP-level proof of the Phase 4 chat channel: first-time customer
identification (no customer_id, just name+contact), the clarification
round-trip, and the rate limiter actually being wired into the route --
all with FakeInterpreter / a small stub, no network call.
"""

from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient

from app.ai.dependency import get_interpreter
from app.ai.providers.fake import FakeInterpreter
from app.ai.schemas import AIInvocationMetadata, Intent, InterpretationOutcome, InterpretedRequest
from app.api.rate_limit import FixedWindowRateLimiter, get_chat_rate_limiter
from app.domain.models import Business, Service, StaffResource
from app.main import app


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


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.pop(get_interpreter, None)
    app.dependency_overrides.pop(get_chat_rate_limiter, None)


@pytest.mark.asyncio
async def test_first_time_customer_chat_and_clarification_via_http(
    business: Business, service: Service, staff: StaffResource
) -> None:
    stub = _SequencedStubInterpreter(
        [
            InterpretedRequest(
                intent=Intent.BOOK,
                is_ambiguous=True,
                ambiguity_reason="Book, or asking about hours?",
                confidence=0.4,
            ),
            InterpretedRequest(
                intent=Intent.BOOK, service_hint="Haircut", is_ambiguous=False, confidence=0.9
            ),
        ]
    )
    app.dependency_overrides[get_interpreter] = lambda: stub

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/requests",
            json={
                "business_id": str(business.id),
                "customer_name": "Jamie Lee",
                "customer_contact": "jamie@example.com",
                "message": "Can I come Friday?",
            },
        )
        assert create_resp.status_code == 201
        run = create_resp.json()
        assert run["state"] == "AWAITING_CLARIFICATION"
        assert run["customer_id"]  # resolved to a real customer with no customer_id supplied

        reply_resp = await client.post(
            f"/v1/requests/{run['id']}/reply", json={"message": "I want to book a Haircut"}
        )
        assert reply_resp.status_code == 200
        replied = reply_resp.json()
        assert replied["state"] == "AWAITING_CONFIRMATION"
        assert replied["offered_slots"]


@pytest.mark.asyncio
async def test_missing_customer_identity_is_rejected(business: Business) -> None:
    # FakeInterpreter, like every other test here -- identity is checked
    # after interpretation (see app/workflow/orchestrator.py), so this
    # route still resolves get_interpreter via DI before the 422 fires.
    app.dependency_overrides[get_interpreter] = lambda: FakeInterpreter()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/requests",
            json={"business_id": str(business.id), "message": "Can I come Friday?"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rate_limit_enforced_on_public_endpoint(business: Business) -> None:
    # FakeInterpreter here too -- this test is only exercising the rate
    # limiter dependency, not interpretation, and must never fall through
    # to the real (paid) provider.
    app.dependency_overrides[get_interpreter] = lambda: FakeInterpreter()
    # Instantiated once, outside the lambda -- a lambda that constructs a
    # new limiter per call would never accumulate state across requests,
    # which is exactly the bug this test caught on the first pass.
    test_limiter = FixedWindowRateLimiter(max_requests=1, window_seconds=60)
    app.dependency_overrides[get_chat_rate_limiter] = lambda: test_limiter

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/requests",
            json={
                "business_id": str(business.id),
                "customer_name": "A",
                "customer_contact": "a@example.com",
                "message": "hello",
            },
        )
        second = await client.post(
            "/v1/requests",
            json={
                "business_id": str(business.id),
                "customer_name": "B",
                "customer_contact": "b@example.com",
                "message": "hello",
            },
        )

    assert first.status_code == 201
    assert second.status_code == 429
