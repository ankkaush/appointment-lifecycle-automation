import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_run_tick_endpoint_returns_summary() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/jobs/run-tick")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"reminders_sent", "no_shows_detected", "runs_expired"}
    assert all(isinstance(v, int) for v in body.values())
