"""Phase 9: the escalation endpoints are the first real dashboard
consumer, and used to return/act on every business's escalations
regardless of caller -- these tests prove the business_id scoping fix
actually holds, not just that the happy path still works.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.dependency import get_interpreter
from app.ai.providers.fake import FakeInterpreter
from app.domain.models import Business, Customer
from app.main import app


@pytest.fixture(autouse=True)
def _override_interpreter():
    app.dependency_overrides[get_interpreter] = lambda: FakeInterpreter()
    yield
    app.dependency_overrides.pop(get_interpreter, None)


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
    other_business = Business(name="Other Salon", timezone="America/New_York")
    db.add(other_business)
    await db.flush()  # other_business.id is assigned at flush, not construction
    other_customer = Customer(
        business_id=other_business.id, name="Other Customer", contact="other@example.com"
    )
    db.add(other_customer)
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_a = await _escalate(client, business, customer)
        run_b = await _escalate(client, other_business, other_customer)

        resp_a = await client.get("/v1/escalations", params={"business_id": str(business.id)})
        ids_a = {c["processing_run_id"] for c in resp_a.json()}
        assert run_a["id"] in ids_a
        assert run_b["id"] not in ids_a

        resp_b = await client.get("/v1/escalations", params={"business_id": str(other_business.id)})
        ids_b = {c["processing_run_id"] for c in resp_b.json()}
        assert run_b["id"] in ids_b
        assert run_a["id"] not in ids_b


@pytest.mark.asyncio
async def test_escalation_list_requires_business_id(business: Business) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/escalations")
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_resolve_escalation_rejected_for_the_wrong_business(
    db: AsyncSession, business: Business, customer: Customer
) -> None:
    other_business = Business(name="Other Salon", timezone="America/New_York")
    db.add(other_business)
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run = await _escalate(client, business, customer)
        cases = (
            await client.get("/v1/escalations", params={"business_id": str(business.id)})
        ).json()
        case_id = next(c["id"] for c in cases if c["processing_run_id"] == run["id"])

        resolve_resp = await client.post(
            f"/v1/escalations/{case_id}/resolve",
            params={"business_id": str(other_business.id)},
        )
        assert resolve_resp.status_code == 404

        # Still OPEN -- the wrong-business attempt didn't resolve it.
        still_open = (
            await client.get(
                "/v1/escalations",
                params={"business_id": str(business.id), "status_filter": "OPEN"},
            )
        ).json()
        assert any(c["id"] == case_id for c in still_open)
