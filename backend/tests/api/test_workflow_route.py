"""End-to-end HTTP proof of the Phase 3 lifecycle, with FakeInterpreter
swapped in -- no network call, matching the same discipline as
test_interpret_route.py.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.ai.dependency import get_interpreter
from app.ai.providers.fake import FakeInterpreter
from app.domain.models import Business, Customer, Service, StaffResource
from app.main import app


@pytest.fixture(autouse=True)
def _override_interpreter():
    app.dependency_overrides[get_interpreter] = lambda: FakeInterpreter()
    yield
    app.dependency_overrides.pop(get_interpreter, None)


@pytest.mark.asyncio
async def test_full_lifecycle_via_http(
    business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/requests",
            json={
                "business_id": str(business.id),
                "customer_id": str(customer.id),
                "message": "I'd like to book a Haircut appointment",
            },
        )
        assert create_resp.status_code == 201
        run = create_resp.json()
        assert run["state"] == "AWAITING_CONFIRMATION"
        assert run["offered_slots"]

        chosen = run["offered_slots"][0]
        confirm_resp = await client.post(
            f"/v1/requests/{run['id']}/confirm",
            json={"start_at": chosen["start_at"], "idempotency_key": "http-test-1"},
        )
        assert confirm_resp.status_code == 200
        confirmed = confirm_resp.json()
        assert confirmed["state"] == "SUCCEEDED"
        assert confirmed["resulting_appointment_id"] is not None

        trace_resp = await client.get(f"/v1/requests/{run['id']}/trace")
        assert trace_resp.status_code == 200
        trace = trace_resp.json()
        assert trace["ai_invocation"]["intent"] == "book"
        step_names = [s["step_name"] for s in trace["steps"]]
        assert step_names == [
            "received",
            "interpreted",
            "slots_offered",
            "awaiting_confirmation",
            "booking_attempted",
            "booking_succeeded",
            "calendar_sync_succeeded",
            "confirmation_sent",
        ]


@pytest.mark.asyncio
async def test_escalation_flow_via_http(business: Business, customer: Customer) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/requests",
            json={
                "business_id": str(business.id),
                "customer_id": str(customer.id),
                "message": "asdkfj qwer 1234",
            },
        )
        run = create_resp.json()
        assert run["state"] == "ESCALATED"

        list_resp = await client.get("/v1/escalations", params={"status_filter": "OPEN"})
        assert list_resp.status_code == 200
        cases = list_resp.json()
        matching = [c for c in cases if c["processing_run_id"] == run["id"]]
        assert len(matching) == 1

        resolve_resp = await client.post(f"/v1/escalations/{matching[0]['id']}/resolve")
        assert resolve_resp.status_code == 200
        assert resolve_resp.json()["status"] == "RESOLVED"
