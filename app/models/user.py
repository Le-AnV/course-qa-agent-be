"""Users — every account is created by an admin, no self-registration (AC-03)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ROLE_ADMIN = "admin"
ROLE_TRAINER = "trainer"
ROLE_TA = "teaching_assistant"
ROLE_STUDENT = "student"
VALID_ROLES = (ROLE_ADMIN, ROLE_TRAINER, ROLE_TA, ROLE_STUDENT)
# Aliases accepted in the .xlsx `type` column (AC-05).
ROLE_ALIASES = {
    "student": ROLE_STUDENT,
    "trainer": ROLE_TRAINER,
    "teaching assistant": ROLE_TA,
    "teaching_assistant": ROLE_TA,
    "ta": ROLE_TA,
    "assistant": ROLE_TA,
}
# Roles that must be assigned to a class before they can log in (AC-03).
ROLES_REQUIRING_CLASS = (ROLE_TRAINER, ROLE_TA)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    class_id: Mapped[int | None] = mapped_column(
        ForeignKey("classes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
