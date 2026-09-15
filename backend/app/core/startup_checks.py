"""Fail fast, not on the first request. When APP_ENV=production, refuses
to boot with a dev-only default still in place -- a wrong config
discovered at `docker run` time is a five-second fix; the same thing
discovered when the first real customer message hits an unset API key,
or a session hijack is possible because SECRET_KEY was never changed, is
an incident. Every other environment (the local-dev default) skips this
entirely -- these defaults exist specifically so local development needs
no configuration at all.
"""

from __future__ import annotations

from app.core.config import Settings

DEV_SECRET_KEY = "dev-secret-do-not-use-in-production"
DEV_DASHBOARD_ORIGIN = "http://localhost:3010"


class InsecureProductionConfigError(RuntimeError):
    pass


def validate_production_settings(settings: Settings) -> None:
    if settings.app_env != "production":
        return

    problems: list[str] = []

    if settings.secret_key == DEV_SECRET_KEY:
        problems.append("SECRET_KEY is still the local-dev default")
    if "changeme" in settings.database_url:
        problems.append("DATABASE_URL still uses the local-dev placeholder password")
    if settings.dashboard_origin == DEV_DASHBOARD_ORIGIN:
        problems.append("DASHBOARD_ORIGIN is still the local-dev default (http://localhost:3010)")
    if not settings.anthropic_api_key:
        problems.append("ANTHROPIC_API_KEY is not set")
    if settings.notification_provider == "resend" and (
        not settings.resend_api_key or not settings.resend_from_email
    ):
        problems.append(
            "NOTIFICATION_PROVIDER=resend but RESEND_API_KEY / RESEND_FROM_EMAIL isn't set"
        )

    if problems:
        raise InsecureProductionConfigError(
            "Refusing to start with APP_ENV=production and insecure/incomplete config:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
