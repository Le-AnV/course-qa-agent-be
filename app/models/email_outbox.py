"""Email outbox queue — rate-limited sending so a class import never hits Gmail limits (AC-09)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EMAIL_PENDING = "pending"
EMAIL_SENT = "sent"
EMAIL_FAILED = "failed"

EMAIL_STATUSES = (EMAIL_PENDING, EMAIL_SENT, EMAIL_FAILED)


class EmailOutbox(Base):
    __tablename__ = "email_outbox"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    to_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    # Rendered body. Plaintext credential emails only — no password is ever
    # written anywhere else (no xlsx column, never returned by the API).
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
