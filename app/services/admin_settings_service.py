"""Build safe, read-only system settings for the Admin dashboard."""

from app.config import Settings
from app.schemas.admin import (
    AdminSettingsRead,
    EmailSettingsRead,
    LlmSettingsRead,
)


def build_admin_settings(
    config: Settings,
) -> AdminSettingsRead:
    """Return non-sensitive settings and credential readiness indicators."""

    return AdminSettingsRead(
        llm=LlmSettingsRead(
            model=config.llm_model,
            api_key_configured=config.google_api_key is not None,
        ),
        email=EmailSettingsRead(
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            smtp_security=config.smtp_security,
            sender_email=config.smtp_sender_email,
            rate_limit_per_minute=config.email_rate_limit_per_minute,
            app_password_configured=config.smtp_app_password is not None,
        ),
    )
