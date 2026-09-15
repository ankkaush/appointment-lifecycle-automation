"""Phase 9/10: the escalation endpoints are the first real dashboard
consumer. Phase 9 scoped them by business_id; Phase 10 layers that
business's API key on top -- these tests prove both actually hold, not
just that the happy path still works.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.dependency import get_interpreter
from app.ai.providers.fake import FakeInterpreter
from app.api.auth import generate_api_key, hash_api_key
from app.domain.models import Business, Customer
from app.main import app


@pytest.fixture(autouse=True)
def _override_interpreter():
    app.dependency_overrides[get_interpreter] = lambda: FakeInterpreter()
    yield
    app.dependency_overrides.pop(get_interpreter, None)


def _auth(business: Business) -> dict[str, str]:
    return {"Authorization": f"Bearer {business.api_key}"}


async def _escalate(client: AsyncClient, business: Business, customer: Customer) -> dict:
    resp = await client.post(
        "/v1/requests",
        json={
            "business_id": str(business.id),
            "customer_id": str(customer.id),
            "message": "asdkfj qwer 1234",
        },
    )
    run = resp.json()
    assert run["state"] == "ESCALATED"
    return run


@pytest.mark.asyncio
async def test_escalation_list_is_scoped_to_the_requested_business(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    other_key = generate_api_key()
    other_business = Business(
        name="Other Salon", timezone="America/New_York", api_key_hash=hash_api_key(other_key)
    )
    db.add(other_business)
    await db.flush()  # other_business.id is assigned at flush, not construction
    other_customer = Customer(
        business_id=other_business.id, name="Other Customer", contact="other@example.com"
    )
    db.add(other_customer)
    await db.commit()
    other_business.api_key = other_key

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_a = await _escalate(client, business, customer)
        run_b = await _escalate(client, other_business, other_customer)

        resp_a = await client.get(
            "/v1/escalations", params={"business_id": str(business.id)}, headers=_auth(business)
        )
        ids_a = {c["processing_run_id"] for c in resp_a.json()}
        assert run_a["id"] in ids_a
        assert run_b["id"] not in ids_a

        resp_b = await client.get(
            "/v1/escalations",
            params={"business_id": str(other_business.id)},
            headers=_auth(other_business),
        )
        ids_b = {c["processing_run_id"] for c in resp_b.json()}
        assert run_b["id"] in ids_b
        assert run_a["id"] not in ids_b


@pytest.mark.asyncio
async def test_escalation_list_requires_business_id(business: Business) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/escalations", headers=_auth(business))
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_escalation_list_rejects_missing_api_key(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _escalate(client, business, customer)
        resp = await client.get("/v1/escalations", params={"business_id": str(business.id)})
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_escalation_list_rejects_wrong_api_key(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _escalate(client, business, customer)
        resp = await client.get(
            "/v1/escalations",
            params={"business_id": str(business.id)},
            headers={"Authorization": "Bearer not-the-real-key"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_resolve_escalation_rejected_for_the_wrong_business(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    other_key = generate_api_key()
    other_business = Business(
        name="Other Salon", timezone="America/New_York", api_key_hash=hash_api_key(other_key)
    )
    db.add(other_business)
    await db.commit()
    other_business.api_key = other_key

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run = await _escalate(client, business, customer)
        cases = (
            await client.get(
                "/v1/escalations", params={"business_id": str(business.id)}, headers=_auth(business)
            )
        ).json()
        case_id = next(c["id"] for c in cases if c["processing_run_id"] == run["id"])

        # Legitimately authenticated as other_business -- but the
        # escalation itself belongs to `business`, so it's still 404.
        resolve_resp = await client.post(
            f"/v1/escalations/{case_id}/resolve",
            params={"business_id": str(other_business.id)},
            headers=_auth(other_business),
        )
        assert resolve_resp.status_code == 404

        # Still OPEN -- the wrong-business attempt didn't resolve it.
        still_open = (
            await client.get(
                "/v1/escalations",
                params={"business_id": str(business.id), "status_filter": "OPEN"},
                headers=_auth(business),
            )
        ).json()
        assert any(c["id"] == case_id for c in still_open)
