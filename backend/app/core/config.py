from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment variables / .env.

    No secret ever gets a real default here — only inert placeholders that
    make local development possible without credentials.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = (
        "postgresql+asyncpg://appointment:changeme@localhost:5432/appointment_automation"
    )
    secret_key: str = "dev-secret-do-not-use-in-production"

    anthropic_api_key: str | None = None
    ai_model: str = "claude-haiku-4-5-20251001"

    calendar_provider: str = "mock"
    notification_provider: str = "console"
    resend_api_key: str | None = None
    resend_from_email: str | None = None

    # The Phase 9 dashboard's origin, for CORS -- one value is enough
    # since there's exactly one first-party frontend consuming this API
    # cross-origin (the chat UI is same-origin, mounted directly below).
    dashboard_origin: str = "http://localhost:3010"

    sentry_dsn: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
