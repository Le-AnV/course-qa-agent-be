"""Tests for environment-backed application settings."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_loads_gemini_api_key_as_a_secret(monkeypatch):
    """Load a member's Gemini key as a masked secret value."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret-key")

    settings = Settings(_env_file=None)

    assert settings.google_api_key is not None
    assert settings.google_api_key.get_secret_value() == "test-secret-key"
    assert "test-secret-key" not in repr(settings)


def test_settings_loads_smtp_app_password_as_a_secret(monkeypatch):
    """Load the shared Gmail app password as a masked secret value."""
    monkeypatch.setenv("SMTP_APP_PASSWORD", "test-app-password")

    settings = Settings(_env_file=None)

    assert settings.smtp_app_password is not None
    assert settings.smtp_app_password.get_secret_value() == "test-app-password"
    assert "test-app-password" not in repr(settings)


def test_settings_loads_smtp_sender_email_from_environment(monkeypatch):
    """Load and validate the shared sender address from the environment."""
    monkeypatch.setenv("SMTP_SENDER_EMAIL", "sender@example.com")

    settings = Settings(_env_file=None)

    assert settings.smtp_sender_email == "sender@example.com"


def test_settings_rejects_invalid_smtp_sender_email(monkeypatch):
    """Reject a malformed sender address at configuration load time."""
    monkeypatch.setenv("SMTP_SENDER_EMAIL", "not-an-email")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_allows_missing_optional_secrets(monkeypatch):
    """Allow local runs that have not configured optional service credentials."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)

    settings = Settings(_env_file=None)

    assert settings.google_api_key is None
    assert settings.smtp_app_password is None


def test_settings_treats_blank_optional_environment_values_as_missing(monkeypatch):
    """Normalize blank environment values to unset optional credentials."""
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.setenv("SMTP_SENDER_EMAIL", "")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "   ")

    settings = Settings(_env_file=None)

    assert settings.google_api_key is None
    assert settings.smtp_sender_email is None
    assert settings.smtp_app_password is None


def test_settings_uses_confirmed_ai105_defaults():
    """Keep the agreed model, SMTP transport, and send-rate values fixed."""
    settings = Settings(_env_file=None)

    assert settings.llm_model == "models/gemini-3.5-flash-lite"
    assert settings.smtp_host == "smtp.gmail.com"
    assert settings.smtp_port == 587
    assert settings.smtp_security == "STARTTLS"
    assert settings.email_rate_limit_per_minute == 20


def test_environment_cannot_override_fixed_ai105_configuration(monkeypatch):
    """Prevent environment variables from changing code-fixed AI-105 settings."""
    monkeypatch.setenv("LLM_MODEL", "models/another-model")
    monkeypatch.setenv("SMTP_PORT", "465")

    settings = Settings(_env_file=None)

    assert settings.llm_model == "models/gemini-3.5-flash-lite"
    assert settings.smtp_port == 587
