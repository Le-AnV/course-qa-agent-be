"""Tests for assembling safe Admin settings responses."""

from app.config import Settings
from app.services.admin_settings_service import build_admin_settings


def test_build_admin_settings_returns_confirmed_values_without_secrets():
    """Expose confirmed setting values and configured flags, never secrets."""
    config = Settings(
        _env_file=None,
        google_api_key="gemini-secret",
        smtp_sender_email="sender@example.com",
        smtp_app_password="smtp-secret",
    )

    response = build_admin_settings(config)
    payload = response.model_dump(mode="json")

    assert payload == {
        "llm": {
            "model": "models/gemini-3.8-flash",
            "api_key_configured": True,
        },
        "email": {
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_security": "STARTTLS",
            "sender_email": "sender@example.com",
            "rate_limit_per_minute": 20,
            "app_password_configured": True,
        },
    }
    assert "gemini-secret" not in response.model_dump_json()
    assert "smtp-secret" not in response.model_dump_json()


def test_build_admin_settings_reports_missing_optional_credentials():
    """Report unset credentials as not configured without raising an error."""
    config = Settings(_env_file=None)

    response = build_admin_settings(config)

    assert response.llm.api_key_configured is False
    assert response.email.sender_email is None
    assert response.email.app_password_configured is False
