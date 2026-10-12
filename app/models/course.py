"""Courses and class sections (AC-01, AC-02)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ClassSection(Base):
    """A section under a course. AC-01: created only under an existing course."""

    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # Exactly one trainer per class (AC-02); nullable until assigned — a class
    # cannot be *given* to anyone before it exists, assignment happens after.
    # NOTE: users.class_id points back at classes (cycle). Plain FKs are kept
    # on purpose: use_alter=True would emit ALTER ... ADD CONSTRAINT, which
    # SQLite cannot execute. SQLAlchemy sorts around the cycle on its own
    # (may log a SAWarning on drop_all, harmless on Postgres/SQLite).
    trainer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ta_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("course_id", "code", name="uq_class_course_code"),)
