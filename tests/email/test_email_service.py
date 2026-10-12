"""Tests for secure SMTP test-email delivery."""

import smtplib

import pytest

from app.config import Settings
from app.services.email_service import (
    EmailConfigurationError,
    EmailDeliveryError,
    send_email,
    send_test_email,
)


class FakeSmtpClient:
    """Record SMTP behavior without opening a network connection."""

    def __init__(self, host, port, timeout):
        """Store connection arguments for assertions without a network call."""
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_count = 0
        self.tls_context = None
        self.login_credentials = None
        self.message = None

    def __enter__(self):
        """Make the fake SMTP client usable in a context manager."""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Allow the SMTP context manager to finish without suppressing errors."""
        return False

    def ehlo(self):
        """Count SMTP capability negotiation calls."""
        self.ehlo_count += 1

    def starttls(self, *, context):
        """Capture the TLS context provided by the email service."""
        self.tls_context = context

    def login(self, username, password):
        """Capture the credentials passed to the SMTP client."""
        self.login_credentials = (username, password)

    def send_message(self, message):
        """Capture the outgoing email message for assertions."""
        self.message = message


def test_send_test_email_uses_starttls_and_configured_credentials(monkeypatch):
    """Send a test message over STARTTLS using the configured shared sender."""
    clients = []

    def create_client(host, port, timeout):
        """Return a fake SMTP client and retain it for message assertions."""
        client = FakeSmtpClient(host, port, timeout)
        clients.append(client)
        return client

    tls_context = object()
    monkeypatch.setattr("app.services.email_service.smtplib.SMTP", create_client)
    monkeypatch.setattr(
        "app.services.email_service.ssl.create_default_context", lambda: tls_context
    )
    config = Settings(
        _env_file=None,
        smtp_sender_email="sender@example.com",
        smtp_app_password="smtp-secret",
    )

    result = send_test_email("admin@example.com", config)

    client = clients[0]
    assert result.success is True
    assert client.host == "smtp.gmail.com"
    assert client.port == 587
    assert client.ehlo_count == 2
    assert client.tls_context is tls_context
    assert client.login_credentials == ("sender@example.com", "smtp-secret")
    assert client.message["From"] == "sender@example.com"
    assert client.message["To"] == "admin@example.com"
    assert client.message["Subject"] == "Course Q&A Agent test email"


@pytest.mark.parametrize(
    ("sender_email", "app_password"),
    [
        (None, "smtp-secret"),
        ("sender@example.com", None),
    ],
)
def test_send_test_email_rejects_missing_configuration(sender_email, app_password):
    """Reject test-email sends if either shared SMTP credential is absent."""
    config = Settings(
        _env_file=None,
        smtp_sender_email=sender_email,
        smtp_app_password=app_password,
    )

    with pytest.raises(EmailConfigurationError):
        send_test_email("admin@example.com", config)


def test_send_test_email_wraps_smtp_errors_without_exposing_secrets(monkeypatch):
    """Return a safe delivery error when the SMTP provider rejects login."""

    class FailingSmtpClient(FakeSmtpClient):
        def login(self, username, password):
            """Simulate a failed SMTP authentication attempt."""
            raise smtplib.SMTPAuthenticationError(535, b"authentication failed")

    monkeypatch.setattr("app.services.email_service.smtplib.SMTP", FailingSmtpClient)
    config = Settings(
        _env_file=None,
        smtp_sender_email="sender@example.com",
        smtp_app_password="smtp-secret",
    )

    with pytest.raises(EmailDeliveryError) as error:
        send_test_email("admin@example.com", config)

    assert str(error.value) == "Test email could not be sent"
    assert "smtp-secret" not in str(error.value)


def test_send_email_delivers_account_message_through_configured_sender(monkeypatch):
    """Deliver a normal account email through the shared SMTP configuration."""

    clients = []

    def create_client(host, port, timeout):
        """Return a fake SMTP client and retain it for message assertions."""
        """Create and retain a fake SMTP client for message assertions."""

        client = FakeSmtpClient(host, port, timeout)
        clients.append(client)
        return client

    monkeypatch.setattr("app.services.email_service.smtplib.SMTP", create_client)
    monkeypatch.setattr(
        "app.services.email_service.ssl.create_default_context", lambda: object()
    )
    config = Settings(
        _env_file=None,
        smtp_sender_email="sender@example.com",
        smtp_app_password="smtp-secret",
    )

    send_email(
        "student@example.com",
        "Your Course QA account",
        "Your temporary password is: example-only",
        config,
    )

    message = clients[0].message
    assert message["From"] == "sender@example.com"
    assert message["To"] == "student@example.com"
    assert message["Subject"] == "Your Course QA account"
    assert "example-only" in message.get_content()
