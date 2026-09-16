"""Proves the invariant tests/conftest.py exists to enforce: running
pytest can never delete or modify the development/demo database. Not a
one-off manual check -- a permanent regression test, so this can't
silently regress the way it broke once already.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from tests.conftest import TEST_DATABASE_URL


def test_test_database_url_is_never_the_apps_own_database_url() -> None:
    app_database_url = get_settings().database_url
    assert TEST_DATABASE_URL != app_database_url
    assert TEST_DATABASE_URL.startswith(app_database_url.rsplit("/", 1)[0] + "/")
    assert TEST_DATABASE_URL.endswith("_test")


@pytest.mark.asyncio
async def test_pytest_is_actually_connected_to_the_test_database(db: AsyncSession) -> None:
    """Not just "the URLs differ on paper" -- proves the live session a
    test actually runs queries through is really connected to the
    dedicated test database, by asking Postgres itself."""
    current_database = await db.scalar(text("SELECT current_database()"))
    app_database_name = get_settings().database_url.rsplit("/", 1)[1]

    assert current_database != app_database_name
    assert current_database == app_database_name + "_test"
