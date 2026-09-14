"""Exercises the /v1/interpret route end to end through the real HTTP
layer — but with FakeInterpreter swapped in via dependency override, so
this suite never makes a network call or needs an API key. Proving the
real Anthropic integration is the golden-set eval's job (eval/run_eval.py),
not pytest's.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.ai.dependency import get_interpreter
from app.ai.providers.fake import FakeInterpreter
from app.domain.models import Business, Service
from app.main import app


@pytest.fixture(autouse=True)
def _override_interpreter():
    app.dependency_overrides[get_interpreter] = lambda: FakeInterpreter()
    yield
    app.dependency_overrides.pop(get_interpreter, None)


@pytest.mark.asyncio
async def test_interpret_book_request_matches_known_service(
    business: Business, service: Service
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/interpret",
            json={
                "business_id": str(business.id),
                "message": "I'd like to book a Haircut next Tuesday afternoon",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["request"]["intent"] == "book"
    assert body["matched_service"] == service.name
    assert body["metadata"]["provider"] == "fake"


@pytest.mark.asyncio
async def test_interpret_unrecognized_message_is_flagged_ambiguous(business: Business) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/interpret",
            json={"business_id": str(business.id), "message": "asdkfj qwer 1234"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["request"]["intent"] == "unknown"
    assert body["request"]["is_ambiguous"] is True
    assert body["matched_service"] is None


@pytest.mark.asyncio
async def test_interpret_cancel_request(business: Business) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/interpret",
            json={"business_id": str(business.id), "message": "Can I cancel my appointment?"},
        )

    assert response.status_code == 200
    assert response.json()["request"]["intent"] == "cancel"
