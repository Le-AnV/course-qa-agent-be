"""Send Admin test emails through the configured STARTTLS SMTP server."""

import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings
from app.schemas.admin import ConnectionTestResult

SMTP_TIMEOUT_SECONDS = 10


class EmailConfigurationError(RuntimeError):
    """Raised when required SMTP credentials are not configured."""


class EmailDeliveryError(RuntimeError):
    """Raised when the SMTP provider rejects or cannot deliver the test email."""


def _send_message(message: EmailMessage, config: Settings, failure_message: str) -> None:
    """Send one message via STARTTLS without returning SMTP credentials."""
    sender = config.smtp_sender_email
    password = config.smtp_app_password
    if sender is None or password is None:
        raise EmailConfigurationError("SMTP sender email and app password are required")

    try:
        with smtplib.SMTP(
            config.smtp_host,
            config.smtp_port,
            timeout=SMTP_TIMEOUT_SECONDS,
        ) as smtp_client:
            smtp_client.ehlo()
            smtp_client.starttls(context=ssl.create_default_context())
            smtp_client.ehlo()
            smtp_client.login(str(sender), password.get_secret_value())
            smtp_client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise EmailDeliveryError(failure_message) from error


def send_email(
    recipient: str,
    subject: str,
    body: str,
    config: Settings,
) -> None:
    """Send a plain-text account or ticket email using the shared SMTP sender."""

    message = EmailMessage()
    message["From"] = str(config.smtp_sender_email or "")
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    _send_message(message, config, "Email could not be sent")


def send_test_email(recipient: str, config: Settings) -> ConnectionTestResult:
    """Send one test email without returning or logging SMTP credentials."""

    message = EmailMessage()
    message["From"] = str(config.smtp_sender_email or "")
    message["To"] = recipient
    message["Subject"] = "Course Q&A Agent test email"
    message.set_content("Your Course Q&A Agent email configuration is working.")
    _send_message(message, config, "Test email could not be sent")

    return ConnectionTestResult(
        success=True,
        message="Test email sent successfully",
    )
