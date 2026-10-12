"""Tests for Admin-managed system settings schemas."""

import pytest
from pydantic import ValidationError

from app.schemas.admin import (
    AdminSettingsRead,
    ConnectionTestResult,
    EmailSettingsRead,
    LlmSettingsRead,
)


# Read-only contracts expose status without accepting or returning secrets.
def test_llm_settings_read_does_not_expose_api_key():
    """Keep the raw Gemini API key out of the Admin settings contract."""
    response = LlmSettingsRead(
        model="gemini-3.8-flash",
        api_key_configured=True,
    )

    assert response.model_dump() == {
        "model": "gemini-3.8-flash",
        "api_key_configured": True,
    }
    assert "api_key" not in response.model_dump()


def test_email_settings_read_does_not_expose_app_password():
    """Keep the SMTP app password out of the Admin settings contract."""
    response = EmailSettingsRead(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_security="STARTTLS",
        sender_email="sender@example.com",
        rate_limit_per_minute=20,
        app_password_configured=True,
    )

    assert response.model_dump() == {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_security": "STARTTLS",
        "sender_email": "sender@example.com",
        "rate_limit_per_minute": 20,
        "app_password_configured": True,
    }
    assert "app_password" not in response.model_dump()


def test_email_settings_read_allows_missing_sender_email():
    """Allow the API to report email settings before SMTP is configured."""
    response = EmailSettingsRead(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_security="STARTTLS",
        sender_email=None,
        rate_limit_per_minute=20,
        app_password_configured=False,
    )

    assert response.sender_email is None
    assert response.app_password_configured is False


def test_admin_settings_read_groups_all_sections():
    """Group the LLM and email configuration under one response contract."""
    response = AdminSettingsRead(
        llm=LlmSettingsRead(model="gemini-3.8-flash", api_key_configured=True),
        email=EmailSettingsRead(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_security="STARTTLS",
            sender_email="sender@example.com",
            rate_limit_per_minute=20,
            app_password_configured=True,
        ),
    )

    assert set(response.model_dump()) == {"llm", "email"}


def test_connection_test_result_rejects_blank_message():
    """Require a useful status message for an external connection check."""
    with pytest.raises(ValidationError):
        ConnectionTestResult(success=False, message="")
