"""Central settings — thresholds and connection strings live here, never hardcoded."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    cors_origins: list[str] = ["http://localhost:3000"]
    database_url: str = "postgresql+psycopg://qa:qa@localhost:5432/course_qa"
    qdrant_url: str = "http://localhost:6333"

    # --- Auth (E1 / AI-25, AI-27) ---
    jwt_secret: str = "dev-only-secret-change-me-use-env-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12
    # Bootstrap admin so a fresh DB is usable before any admin exists.
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "Admin123!"

    # --- Email sender (E8 / AI-105, AC-08) ---
    # App password (Gmail / Google Workspace), never a personal password,
    # never hardcoded — env or admin-configured via API (stored, masked).
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_sender: str = ""
    smtp_app_password: str = ""
    smtp_rate_per_minute: int = 20
    # When true, emails are recorded in the outbox but not actually sent
    # (useful for local dev / tests without Gmail credentials).
    email_dry_run: bool = True


settings = Settings()
