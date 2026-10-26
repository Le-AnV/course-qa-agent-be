"""Build safe, read-only system settings for the Admin dashboard."""

from sqlalchemy.orm import Session

from app.config import Settings
from app.models.gemini_key import GEMINI_KEY_ACTIVE, GeminiApiKey
from app.schemas.admin import (
    AdminSettingsRead,
    EmailSettingsRead,
    LlmSettingsRead,
)
from app.services import gemini_key_service


def build_admin_settings(
    config: Settings,
    db: Session | None = None,
) -> AdminSettingsRead:
    """Return non-sensitive settings and credential readiness indicators."""

    active_keys = 0
    model = config.llm_model
    if db is not None:
        active_keys = (
            db.query(GeminiApiKey).filter(GeminiApiKey.status == GEMINI_KEY_ACTIVE).count()
        )
        model = gemini_key_service.get_chat_model(db)
    return AdminSettingsRead(
        llm=LlmSettingsRead(
            model=model,
            api_key_configured=config.google_api_key is not None or active_keys > 0,
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
