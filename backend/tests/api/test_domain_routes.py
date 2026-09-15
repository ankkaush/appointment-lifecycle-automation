"""Phase 10: per-business API keys on the domain CRUD endpoints. These
were previously completely open -- any caller who knew (or guessed) a
business_id could write to that business's services/staff/customers.
This is the first HTTP-level coverage app/api/routes.py has ever had;
these tests prove the auth gate holds across every gated endpoint, not
just that the happy path still works.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Business, Customer, Service, StaffResource
from app.main import app


def _auth(business: Business) -> dict[str, str]:
    return {"Authorization": f"Bearer {business.api_key}"}


def _next_monday_9am_utc(business: Business) -> datetime:
    tz = ZoneInfo(business.timezone)
    today = datetime.now(tz).date()
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    local_9am = datetime.combine(monday, datetime.min.time(), tzinfo=tz).replace(hour=9)
    return local_9am.astimezone(UTC)


# --- POST /businesses --------------------------------------------------


@pytest.mark.asyncio
async def test_create_business_issues_an_api_key() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/businesses", json={"name": "New Salon", "timezone": "America/New_York"}
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["api_key"]
        assert "api_key_hash" not in body  # only the hash is ever persisted, never returned


# --- Endpoints gated on the business's API key --------------------------


@pytest.mark.asyncio
async def test_create_service_requires_the_business_api_key(business: Business) -> None:
    transport = ASGITransport(app=app)
    payload = {"business_id": str(business.id), "name": "Manicure", "duration_minutes": 30}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post("/v1/services", json=payload, headers=_auth(business))
        assert ok.status_code == 201

        no_key = await client.post("/v1/services", json=payload)
        assert no_key.status_code == 401

        wrong_key = await client.post(
            "/v1/services", json=payload, headers={"Authorization": "Bearer not-the-real-key"}
        )
        assert wrong_key.status_code == 401


@pytest.mark.asyncio
async def test_create_staff_requires_the_business_api_key(business: Business) -> None:
    transport = ASGITransport(app=app)
    payload = {"business_id": str(business.id), "name": "Sam"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post("/v1/staff", json=payload, headers=_auth(business))
        assert ok.status_code == 201

        no_key = await client.post("/v1/staff", json=payload)
        assert no_key.status_code == 401


@pytest.mark.asyncio
async def test_link_staff_service_requires_the_staffs_business_api_key(
    db: AsyncSession, business: Business, staff: StaffResource
) -> None:
    # A fresh, not-yet-linked service -- the `staff` fixture is already
    # linked to the `service` fixture, so reusing that pair here would
    # hit the staff_services primary key on the very insert this test
    # means to prove succeeds.
    other_service = Service(business_id=business.id, name="Beard Trim", duration_minutes=15)
    db.add(other_service)
    await db.commit()
    await db.refresh(other_service)

    transport = ASGITransport(app=app)
    url = f"/v1/staff/{staff.id}/services/{other_service.id}"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        no_key = await client.post(url)
        assert no_key.status_code == 401

        ok = await client.post(url, headers=_auth(business))
        assert ok.status_code == 204


@pytest.mark.asyncio
async def test_add_working_hours_requires_the_staffs_business_api_key(
    business: Business, staff: StaffResource
) -> None:
    transport = ASGITransport(app=app)
    url = f"/v1/staff/{staff.id}/working-hours"
    payload = {"weekday": 1, "start_time": "09:00", "end_time": "17:00"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        no_key = await client.post(url, json=payload)
        assert no_key.status_code == 401

        ok = await client.post(url, json=payload, headers=_auth(business))
        assert ok.status_code == 204


@pytest.mark.asyncio
async def test_create_customer_requires_the_business_api_key(business: Business) -> None:
    transport = ASGITransport(app=app)
    payload = {"business_id": str(business.id), "name": "Alex", "contact": "alex2@example.com"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        no_key = await client.post("/v1/customers", json=payload)
        assert no_key.status_code == 401

        ok = await client.post("/v1/customers", json=payload, headers=_auth(business))
        assert ok.status_code == 201


@pytest.mark.asyncio
async def test_create_appointment_requires_the_business_api_key(
    business: Business,
    service: Service,
    staff: StaffResource,
    customer: Customer,
    idempotency_key: str,
) -> None:
    transport = ASGITransport(app=app)
    payload = {
        "idempotency_key": idempotency_key,
        "business_id": str(business.id),
        "service_id": str(service.id),
        "staff_id": str(staff.id),
        "customer_id": str(customer.id),
        "start_at": _next_monday_9am_utc(business).isoformat(),
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        no_key = await client.post("/v1/appointments", json=payload)
        assert no_key.status_code == 401

        ok = await client.post("/v1/appointments", json=payload, headers=_auth(business))
        assert ok.status_code == 201


# --- POST /availability stays open (read-only, non-sensitive) ----------


@pytest.mark.asyncio
async def test_availability_does_not_require_an_api_key(
    business: Business, service: Service, staff: StaffResource
) -> None:
    transport = ASGITransport(app=app)
    window_start = _next_monday_9am_utc(business)
    window_end = window_start + timedelta(days=1)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/availability",
            params={
                "business_id": str(business.id),
                "service_id": str(service.id),
                "staff_id": str(staff.id),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
            },
        )
        assert resp.status_code == 200


# --- POST /businesses/{id}/rotate-api-key --------------------------------


@pytest.mark.asyncio
async def test_rotate_api_key_bootstraps_a_legacy_business_with_no_key(db: AsyncSession) -> None:
    """A business created before this column existed (or via the ORM
    fixtures directly, bypassing create_business) has no key at all --
    rotate-api-key allows exactly one unauthenticated call to issue its
    first one."""
    legacy = Business(name="Legacy Salon", timezone="America/New_York")
    db.add(legacy)
    await db.commit()
    await db.refresh(legacy)
    assert legacy.api_key_hash is None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/v1/businesses/{legacy.id}/rotate-api-key")
        assert resp.status_code == 200
        first_key = resp.json()["api_key"]
        assert first_key

        # The bootstrap window is one-time: a second unauthenticated call
        # now fails, since the business has a real key.
        second = await client.post(f"/v1/businesses/{legacy.id}/rotate-api-key")
        assert second.status_code == 401


@pytest.mark.asyncio
async def test_rotate_api_key_requires_the_current_key_and_invalidates_the_old_one(
    business: Business,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        wrong = await client.post(
            f"/v1/businesses/{business.id}/rotate-api-key",
            headers={"Authorization": "Bearer not-the-real-key"},
        )
        assert wrong.status_code == 401

        rotated = await client.post(
            f"/v1/businesses/{business.id}/rotate-api-key", headers=_auth(business)
        )
        assert rotated.status_code == 200
        new_key = rotated.json()["api_key"]
        assert new_key != business.api_key

        # The old key no longer authenticates anything.
        stale = await client.post(
            "/v1/services",
            json={"business_id": str(business.id), "name": "Manicure", "duration_minutes": 30},
            headers=_auth(business),
        )
        assert stale.status_code == 401

        # The new key does.
        fresh = await client.post(
            "/v1/services",
            json={"business_id": str(business.id), "name": "Manicure", "duration_minutes": 30},
            headers={"Authorization": f"Bearer {new_key}"},
        )
        assert fresh.status_code == 201
