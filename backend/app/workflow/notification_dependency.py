"""Composition root for the notification layer: the one place that
decides which NotificationService implementation the running app uses,
mirroring app/ai/dependency.py's role for the Interpreter. Both the API
routes and the background worker depend on get_notification_service,
never on a concrete provider -- tests override it (API) or call a Mock/
Console provider directly (worker has no request scope to override).
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.workflow.notifications import (
    ConsoleNotificationProvider,
    MockNotificationProvider,
    NotificationService,
)
from app.workflow.notifications_resend import ResendNotificationProvider


def select_notification_provider(settings: Settings) -> NotificationService:
    """The actual selection logic, factored out from get_notification_service
    so it can be tested directly against a plain Settings object --
    get_notification_service itself is cached and reads the process-global
    settings singleton, which isn't something a test should have to
    monkeypatch just to check "does 'resend' pick the Resend provider."""
    provider = settings.notification_provider

    if provider == "console":
        return ConsoleNotificationProvider()

    if provider == "resend":
        if not settings.resend_api_key or not settings.resend_from_email:
            raise RuntimeError(
                "NOTIFICATION_PROVIDER=resend requires both RESEND_API_KEY and "
                "RESEND_FROM_EMAIL to be set in .env."
            )
        return ResendNotificationProvider(
            api_key=settings.resend_api_key, from_email=settings.resend_from_email
        )

    if provider == "mock":
        # Not the local-dev default (that's "console") -- available for
        # completeness/manual use. Tests import MockNotificationProvider
        # directly rather than going through this dependency at all.
        return MockNotificationProvider()

    raise RuntimeError(
        f"Unknown NOTIFICATION_PROVIDER {provider!r} -- expected 'console', 'resend', or 'mock'."
    )


@lru_cache
def get_notification_service() -> NotificationService:
    return select_notification_provider(get_settings())
