"""Shared test fixtures.

Tests run against a real Postgres, not sqlite and not a rollback-wrapped
transaction — the concurrency test in particular needs genuinely
independent, committing sessions to prove the exclusion constraint holds
under a real race, which a shared-transaction fixture would mask.

Test/dev database isolation: every fixture below uses TEST_DATABASE_URL,
never the app's own settings.database_url directly. See the derivation
and the hard assertion right below -- the incident this exists to prevent
already happened once (pytest's own _clean_tables fixture wiped a live
demo database because tests were run against the same DATABASE_URL the
dev stack was using). The invariant this file now enforces: pytest cannot
point at the same database as the running app, by construction, not by
convention.
"""

from collections.abc import AsyncIterator
from datetime import time
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Imported for its side effect of registering workflow tables on the
# shared Base.metadata, so _schema's create_all covers them regardless of
# which test module runs first.
import app.workflow.models  # noqa: F401,E402
from app.api.auth import generate_api_key, hash_api_key
from app.core.config import get_settings
from app.core.db import engine as app_engine
from app.core.db import get_db as app_get_db
from app.domain.models import (
    Base,
    Business,
    Customer,
    Service,
    StaffResource,
    StaffWorkingHours,
    staff_services,
)
from app.main import app as fastapi_app

settings = get_settings()


def _derive_test_database_url(app_database_url: str) -> str:
    """Same host/user/credentials as the app's own DATABASE_URL, but a
    dedicated database name -- derived, not separately configured, so
    there's no second setting to forget to change. Appending rather than
    replacing means it automatically tracks whatever database name the
    app is actually configured with in any given environment."""
    prefix, _, database_name = app_database_url.rpartition("/")
    if not prefix or not database_name:
        raise RuntimeError(f"Could not parse a database name out of {app_database_url!r}")
    return f"{prefix}/{database_name}_test"


TEST_DATABASE_URL = _derive_test_database_url(settings.database_url)

# The one assertion this whole mechanism rests on. If this ever fires,
# something upstream (a misconfigured DATABASE_URL, a copy-paste in this
# file) has broken the derivation -- refuse to run rather than risk
# repeating the incident this file's docstring describes.
assert TEST_DATABASE_URL != settings.database_url, (
    "TEST_DATABASE_URL must never equal the app's own DATABASE_URL -- "
    "refusing to let pytest run against a potentially live database."
)


async def _ensure_test_database_exists() -> None:
    """Postgres has no CREATE DATABASE IF NOT EXISTS, and CREATE DATABASE
    can't run inside a transaction -- connects to the driver-default
    'postgres' maintenance database (present on every Postgres instance)
    to check for, and if needed create, the dedicated test database.
    Idempotent and cheap enough to call from a function-scoped fixture
    every test, matching this file's existing create_all pattern rather
    than fighting pytest-asyncio's per-test event loop with a
    session-scoped fixture (see _schema's docstring for why that's
    avoided elsewhere in this file)."""
    prefix, _, test_database_name = TEST_DATABASE_URL.rpartition("/")
    # asyncpg.connect wants a plain postgresql:// DSN, not SQLAlchemy's
    # dialect-qualified postgresql+asyncpg:// URL.
    maintenance_dsn = f"{prefix}/postgres".replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(maintenance_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", test_database_name
        )
        if not exists:
            # Can't parametrize an identifier -- test_database_name is
            # derived from our own settings, not external input.
            await conn.execute(f'CREATE DATABASE "{test_database_name}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def _schema() -> AsyncIterator[None]:
    """Function-scoped (not session-scoped) so it always shares the same
    per-test event loop pytest-asyncio's default "function" loop scope
    gives every other fixture and the test itself — session-scoping this
    alone caused a loop mismatch. create_all is idempotent, so re-running
    it every test costs one cheap round trip, not a real rebuild."""
    await _ensure_test_database_exists()
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # Extensions are per-database, not instance-wide -- the real app
        # database got this from alembic/versions/0001_initial_schema.py;
        # a freshly-created test database never runs migrations (it's
        # built straight from Base.metadata via create_all), so it needs
        # this explicitly. Needed for the GiST exclusion constraint on
        # Appointment (staff_id, tstzrange(start_at, end_at)).
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def db(_schema) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(_schema):
    """For tests that need more than one independent session at once
    (the concurrency test) rather than the single shared `db` fixture."""
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _override_app_get_db(_schema) -> AsyncIterator[None]:
    """The bug that caused the incident this file's docstring describes,
    closed at its actual source: app.main's FastAPI app depends on
    app.core.db.get_db, bound to app.core.db.engine -- a module-level
    singleton created from the app's real settings.database_url at
    import time. Every HTTP-level test (ASGITransport(app=app)) was
    therefore hitting the real app database directly, regardless of what
    TEST_DATABASE_URL the fixtures above point at -- overriding it
    globally, for every test, is what actually makes the isolation
    invariant hold for the majority of this suite, not just the tests
    that use the `db` fixture directly."""
    test_engine = create_async_engine(TEST_DATABASE_URL)
    test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def _test_get_db() -> AsyncIterator[AsyncSession]:
        async with test_session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[app_get_db] = _test_get_db
    yield
    fastapi_app.dependency_overrides.pop(app_get_db, None)
    await test_engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _dispose_app_engine() -> AsyncIterator[None]:
    """app.core.db.engine is a process-global singleton, correct for the
    real running app (one process, one event loop) but not for a pytest
    suite where every test function gets its own fresh loop by default.
    Disposed after every test so a pooled asyncpg connection never gets
    checked out under a different loop than the one it was opened on --
    harmless now that get_db is overridden above (this engine is never
    actually queried by test code), but still created at import time, so
    still worth keeping its pool clean between tests."""
    yield
    await app_engine.dispose()


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
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await engine.dispose()


@pytest_asyncio.fixture
async def business(db: AsyncSession) -> Business:
    plaintext_key = generate_api_key()
    obj = Business(
        name="Test Salon",
        timezone="America/New_York",
        min_booking_notice_minutes=0,
        max_booking_horizon_days=90,
        api_key_hash=hash_api_key(plaintext_key),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    # Not a mapped column -- stashed so HTTP-level tests can authenticate
    # as this business without re-deriving a key (only the hash persists).
    obj.api_key = plaintext_key
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


@pytest_asyncio.fixture
async def customer(db: AsyncSession, business: Business) -> Customer:
    obj = Customer(business_id=business.id, name="Alex Rivera", contact="alex@example.com")
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@pytest.fixture
def idempotency_key() -> str:
    return str(uuid4())
