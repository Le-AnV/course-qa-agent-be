"""Per-day chat question usage for student quotas."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChatDailyUsage(Base):
    """Question counter for one student on one local calendar day."""

    __tablename__ = "chat_daily_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
