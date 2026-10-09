"""Email outbox queue — rate-limited sending via Gmail app password (AC-08, AC-09)."""

from __future__ import annotations

import smtplib
import time
from datetime import UTC, datetime
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.config import settings
from app.models.email_outbox import EMAIL_FAILED, EMAIL_PENDING, EMAIL_SENT, EmailOutbox
from app.models.system_config import SystemConfig

CONFIG_KEYS = (
    "smtp_host",
    "smtp_port",
    "smtp_sender",
    "smtp_app_password",
    "smtp_rate_per_minute",
    "email_dry_run",
)
MASKED = "********"


def _config_map(db: Session) -> dict[str, str]:
    rows = db.query(SystemConfig).filter(SystemConfig.key.in_(CONFIG_KEYS)).all()
    return {r.key: r.value for r in rows}


def effective_email_config(db: Session) -> dict:
    """DB-backed config overrides env defaults (admin-configured, AI-105)."""
    stored = _config_map(db)
    host = stored.get("smtp_host", "") or settings.smtp_host
    port_raw = stored.get("smtp_port", "") or str(settings.smtp_port)
    sender = stored.get("smtp_sender", "") or settings.smtp_sender
    app_pw = stored.get("smtp_app_password", "")
    if app_pw == "":
        app_pw = settings.smtp_app_password
    rate_raw = stored.get("smtp_rate_per_minute", "") or str(settings.smtp_rate_per_minute)
    dry_raw = stored.get("email_dry_run", "")
    try:
        port = int(port_raw)
    except ValueError:
        port = settings.smtp_port
    try:
        rate = int(rate_raw)
    except ValueError:
        rate = settings.smtp_rate_per_minute
    rate = max(1, min(500, rate))
    if dry_raw == "":
        dry_run = settings.email_dry_run
    else:
        dry_run = dry_raw.strip().lower() in ("1", "true", "yes", "on")
    return {
        "smtp_host": host,
        "smtp_port": port,
        "smtp_sender": sender,
        "has_app_password": bool(app_pw),
        "smtp_rate_per_minute": rate,
        "email_dry_run": dry_run,
    }


def _smtp_credentials(db: Session) -> tuple[str, int, str, str]:
    cfg = effective_email_config(db)
    stored = _config_map(db)
    app_pw = stored.get("smtp_app_password", "") or settings.smtp_app_password
    return cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_sender"], app_pw


def save_email_config(db: Session, updates: dict) -> dict:
    """Persist admin-provided email settings. App password is write-only."""
    for key in CONFIG_KEYS:
        if key in updates and updates[key] is not None:
            val = str(updates[key])
            # Empty password means "don't change".
            if key == "smtp_app_password" and val == "":
                continue
            row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
            if row is None:
                row = SystemConfig(key=key, value=val)
                db.add(row)
            else:
                row.value = val
    db.commit()
    return effective_email_config(db)


def credential_email_body(username: str, email: str, password: str, class_code: str | None) -> tuple[str, str]:
    subject = "Your Course QA account credentials"
    lines = [
        f"Hello {username},",
        "",
        "An admin created your Course QA Agent account.",
        "",
        f"Login email: {email}",
        f"Temporary password: {password}",
    ]
    if class_code:
        lines.append(f"Class: {class_code}")
    lines += [
        "",
        "Please log in and change your password after first login.",
        "Do not share this email — the admin never sees your password.",
    ]
    return subject, "\n".join(lines)


def reset_email_body(username: str, email: str, password: str) -> tuple[str, str]:
    subject = "Your Course QA account password was reset"
    body = "\n".join(
        [
            f"Hello {username},",
            "",
            "An admin reset your Course QA Agent password.",
            "",
            f"Login email: {email}",
            f"New temporary password: {password}",
            "",
            "Please log in and change it after login.",
        ]
    )
    return subject, body


def queue_email(db: Session, to_email: str, subject: str, body: str, user_id: int | None = None) -> EmailOutbox:
    entry = EmailOutbox(
        to_email=to_email.strip().lower(),
        subject=subject,
        body=body,
        status=EMAIL_PENDING,
        attempts=0,
        last_error="",
        user_id=user_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _send_one(
    host: str,
    port: int,
    sender: str,
    app_password: str,
    to_email: str,
    subject: str,
    body: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    if not sender or not app_password:
        raise RuntimeError("Email sender is not configured — set sender + app password first.")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(sender, app_password)
        smtp.send_message(msg)


def process_queue(db: Session, max_messages: int | None = None, sleep_between: bool = True) -> dict:
    """Send pending emails oldest-first at the configured per-minute rate (AC-09).

    Never sends all at once: delay = 60 / rate_per_minute between messages.
    Returns summary counts. Failures stay in the outbox as `failed` with last_error.
    """
    cfg = effective_email_config(db)
    host, port, sender, app_password = _smtp_credentials(db)
    rate = cfg["smtp_rate_per_minute"]
    delay = 60.0 / rate
    q = db.query(EmailOutbox).filter(EmailOutbox.status == EMAIL_PENDING).order_by(EmailOutbox.id)
    if max_messages is not None:
        q = q.limit(max_messages)
    pending = q.all()
    sent = failed = 0
    for i, entry in enumerate(pending):
        entry.attempts += 1
        try:
            _send_one(host, port, sender, app_password, entry.to_email, entry.subject, entry.body, cfg["email_dry_run"])
            entry.status = EMAIL_SENT
            entry.last_error = ""
            entry.sent_at = datetime.now(UTC)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — recorded per row for the admin report
            entry.status = EMAIL_FAILED
            entry.last_error = str(exc)[:2000]
            failed += 1
        db.commit()
        if sleep_between and i < len(pending) - 1 and delay > 0 and not cfg["email_dry_run"]:
            time.sleep(delay)
    return {"processed": len(pending), "sent": sent, "failed": failed}


def queue_stats(db: Session) -> dict:
    return {
        "pending": db.query(EmailOutbox).filter(EmailOutbox.status == EMAIL_PENDING).count(),
        "sent": db.query(EmailOutbox).filter(EmailOutbox.status == EMAIL_SENT).count(),
        "failed": db.query(EmailOutbox).filter(EmailOutbox.status == EMAIL_FAILED).count(),
    }


def retry_failed(db: Session, email_id: int) -> EmailOutbox:
    """Re-queue one failed email (AC-10: re-send without re-importing)."""
    entry = db.query(EmailOutbox).filter(EmailOutbox.id == email_id).first()
    if entry is None:
        raise ValueError("Email does not exist.")
    if entry.status != EMAIL_FAILED:
        raise ValueError("Only failed emails can be re-queued.")
    entry.status = EMAIL_PENDING
    entry.last_error = ""
    db.commit()
    db.refresh(entry)
    return entry
