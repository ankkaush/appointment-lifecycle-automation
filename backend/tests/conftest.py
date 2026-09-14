"""Shared test fixtures.

Tests run against the real Postgres from docker-compose (via DATABASE_URL),
not sqlite and not a rollback-wrapped transaction — the concurrency test in
particular needs genuinely independent, committing sessions to prove the
exclusion constraint holds under a real race, which a shared-transaction
fixture would mask.
"""

from collections.abc import AsyncIterator
from datetime import time
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.domain.models import (
    Base,
    Business,
    Service,
    StaffResource,
    StaffWorkingHours,
    staff_services,
)

settings = get_settings()


@pytest_asyncio.fixture
async def _schema() -> AsyncIterator[None]:
    """Function-scoped (not session-scoped) so it always shares the same
    per-test event loop pytest-asyncio's default "function" loop scope
    gives every other fixture and the test itself — session-scoping this
    alone caused a loop mismatch. create_all is idempotent, so re-running
    it every test costs one cheap round trip, not a real rebuild."""
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def db(_schema) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(_schema):
    """For tests that need more than one independent session at once
    (the concurrency test) rather than the single shared `db` fixture."""
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(_schema) -> AsyncIterator[None]:
    """Every test gets a clean slate. Runs after each test so a failed
    assertion still leaves the next test isolated.

    Deliberately uses its own connection rather than the test's `db`
    session: reusing a session that the test body already drove (possibly
    leaving it mid-transaction after an error) is what caused an asyncpg
    "another operation is in progress" race here — a fresh connection
    sidesteps that entirely rather than chasing the exact interleaving.
    """
    yield
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await engine.dispose()


@pytest_asyncio.fixture
async def business(db: AsyncSession) -> Business:
    obj = Business(
        name="Test Salon",
        timezone="America/New_York",
        min_booking_notice_minutes=0,
        max_booking_horizon_days=90,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def service(db: AsyncSession, business: Business) -> Service:
    obj = Service(business_id=business.id, name="Haircut", duration_minutes=30, buffer_minutes=15)
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def staff(db: AsyncSession, business: Business, service: Service) -> StaffResource:
    obj = StaffResource(business_id=business.id, name="Jordan")
    db.add(obj)
    await db.flush()
    await db.execute(staff_services.insert().values(staff_id=obj.id, service_id=service.id))
    # Monday-Friday, 09:00-17:00 local time.
    for weekday in range(5):
        db.add(
            StaffWorkingHours(
                staff_id=obj.id,
                weekday=weekday,
                start_time=time(9, 0),
                end_time=time(17, 0),
            )
        )
    await db.commit()
    await db.refresh(obj)
    return obj


@pytest.fixture
def idempotency_key() -> str:
    return str(uuid4())
