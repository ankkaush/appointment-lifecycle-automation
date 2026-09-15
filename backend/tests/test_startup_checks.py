"""Phase 10: the app refuses to boot with a dev default still in place
when APP_ENV=production. Pure-function tests against Settings directly --
no need to actually start the app to prove this logic (and doing so
would need to fight FastAPI's lifespan wiring for no benefit).
"""

import pytest

from app.core.config import Settings
from app.core.startup_checks import (
    InsecureProductionConfigError,
    validate_production_settings,
)

_VALID_PRODUCTION_KWARGS = dict(
    app_env="production",
    secret_key="a-real-random-secret",
    database_url="postgresql+asyncpg://appointment:s3cur3-pw@db.internal:5432/appointment",
    dashboard_origin="https://dashboard.example.com",
    anthropic_api_key="sk-ant-real-key",
    notification_provider="console",
)


def test_passes_with_a_fully_configured_production_settings() -> None:
    settings = Settings(**_VALID_PRODUCTION_KWARGS)
    validate_production_settings(settings)  # does not raise


def test_non_production_app_env_is_never_checked() -> None:
    # Every field below is a dev default -- fine, because app_env isn't
    # "production". This is what makes local dev need zero configuration.
    settings = Settings()
    validate_production_settings(settings)  # does not raise


@pytest.mark.parametrize(
    "override,expected_fragment",
    [
        ({"secret_key": "dev-secret-do-not-use-in-production"}, "SECRET_KEY"),
        (
            {
                "database_url": (
                    "postgresql+asyncpg://appointment:changeme@localhost:5432/appointment"
                )
            },
            "DATABASE_URL",
        ),
        ({"dashboard_origin": "http://localhost:3010"}, "DASHBOARD_ORIGIN"),
        ({"anthropic_api_key": None}, "ANTHROPIC_API_KEY"),
        (
            {"notification_provider": "resend", "resend_api_key": None, "resend_from_email": None},
            "RESEND_API_KEY",
        ),
    ],
)
def test_rejects_each_insecure_or_incomplete_default(
    override: dict, expected_fragment: str
) -> None:
    kwargs = {**_VALID_PRODUCTION_KWARGS, **override}
    settings = Settings(**kwargs)
    with pytest.raises(InsecureProductionConfigError, match=expected_fragment):
        validate_production_settings(settings)


def test_reports_every_problem_at_once() -> None:
    settings = Settings(
        app_env="production",
        secret_key="dev-secret-do-not-use-in-production",
        anthropic_api_key=None,
    )
    with pytest.raises(InsecureProductionConfigError) as exc_info:
        validate_production_settings(settings)
    message = str(exc_info.value)
    assert "SECRET_KEY" in message
    assert "ANTHROPIC_API_KEY" in message
