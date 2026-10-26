"""Admin-managed OpenAI keys for the Decisions scope gate."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

OPENAI_KEY_ACTIVE = "active"
OPENAI_KEY_PAUSED = "paused"
OPENAI_KEY_INVALID = "invalid"


class OpenAIApiKey(Base):
    __tablename__ = "openai_api_key"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    key_last4: Mapped[str] = mapped_column(String(8), nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OPENAI_KEY_ACTIVE, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
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
