"""Admin-managed Gemini API keys."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

GEMINI_KEY_ACTIVE = "active"
GEMINI_KEY_PAUSED = "paused"
GEMINI_KEY_INVALID = "invalid"


class GeminiApiKey(Base):
    __tablename__ = "gemini_api_key"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    account_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    key_last4: Mapped[str] = mapped_column(String(8), nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    # Full model ids returned by models.list(), e.g. "models/gemini-3.5-flash-lite".
    available_models: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=GEMINI_KEY_ACTIVE, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    daily_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_daily_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1500, server_default="1500"
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
