"""Tests for secure SMTP account-email delivery."""

from app.config import Settings
from app.services.email_service import send_email


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
    monkeypatch.setattr("app.services.email_service.ssl.create_default_context", lambda: object())
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
