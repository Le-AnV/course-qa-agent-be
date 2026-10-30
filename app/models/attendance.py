"""SQLAlchemy models for scheduling, attendance, holidays and ticket handover (AI-155).

``class_schedule`` is the recurring template; ``class_session`` is one dated
occurrence materialised from it (or created ad-hoc). The generator itself ships
with AI-136/AI-157; this module owns only the schema contract.
"""

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ATTENDANCE_STATUSES = ("present", "excused_absence", "unexcused_absence", "late")
HANDOVER_REASONS = ("auto_absent", "manual_transfer")
HANDOVER_STATUSES = ("pending", "accepted", "rejected")
HOLIDAY_SCOPES = ("system", "class")
HOLIDAY_TYPES = ("solar", "lunar", "admin")
SESSION_STATUSES = ("scheduled", "cancelled", "rescheduled")

_STUDENT_ONLY = text("student_id IS NOT NULL")
_TRAINER_ONLY = text("trainer_id IS NOT NULL")


class ClassSchedule(Base):
    """Recurring weekly slot: "this class meets Monday 19:00-21:00"."""

    __tablename__ = "class_schedule"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_class_schedule_weekday"),
        CheckConstraint("end_time > start_time", name="ck_class_schedule_times"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_class_schedule_effective",
        ),
        Index("ix_class_schedule_class_weekday", "class_id", "weekday"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False
    )
    # A class IS the subject; the label is copied so old sessions keep their name.
    subject: Mapped[str] = mapped_column(String(100), nullable=False)
    # 0 = Monday ... 6 = Sunday, matching ``datetime.date.weekday()``.
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    trainer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClassSession(Base):
    """One dated occurrence. ``schedule_id`` is NULL for ad-hoc/rescheduled rows."""

    __tablename__ = "class_session"
    __table_args__ = (
        UniqueConstraint("class_id", "date", "start_at", name="uq_class_session_slot"),
        CheckConstraint(
            "status IN ('scheduled', 'cancelled', 'rescheduled')",
            name="ck_class_session_status",
        ),
        CheckConstraint("end_at > start_at", name="ck_class_session_times"),
        Index("ix_class_session_class_date", "class_id", "date"),
        Index("ix_class_session_date", "date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("class_schedule.id", ondelete="SET NULL"), nullable=True, index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trainer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled", server_default="scheduled"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Attendance(Base):
    """One row per person per session — a student OR a trainer, never both."""

    __tablename__ = "attendance"
    __table_args__ = (
        CheckConstraint(
            "(student_id IS NOT NULL) <> (trainer_id IS NOT NULL)",
            name="ck_attendance_person",
        ),
        CheckConstraint(
            "status IN ('present', 'excused_absence', 'unexcused_absence', 'late')",
            name="ck_attendance_status",
        ),
        Index(
            "uq_attendance_session_student",
            "class_session_id",
            "student_id",
            unique=True,
            postgresql_where=_STUDENT_ONLY,
            sqlite_where=_STUDENT_ONLY,
        ),
        Index(
            "uq_attendance_session_trainer",
            "class_session_id",
            "trainer_id",
            unique=True,
            postgresql_where=_TRAINER_ONLY,
            sqlite_where=_TRAINER_ONLY,
        ),
        Index("ix_attendance_student", "student_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class_session.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )
    trainer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Holiday(Base):
    """System-wide or per-class day off, matched by date only."""

    __tablename__ = "holiday"
    __table_args__ = (
        CheckConstraint("type IN ('solar', 'lunar', 'admin')", name="ck_holiday_type"),
        CheckConstraint("scope IN ('system', 'class')", name="ck_holiday_scope"),
        CheckConstraint(
            "(scope = 'class' AND class_id IS NOT NULL)"
            " OR (scope = 'system' AND class_id IS NULL)",
            name="ck_holiday_scope_class",
        ),
        Index(
            "uq_holiday_system_date",
            "date",
            unique=True,
            postgresql_where=text("class_id IS NULL"),
            sqlite_where=text("class_id IS NULL"),
        ),
        Index(
            "uq_holiday_class_date",
            "class_id",
            "date",
            unique=True,
            postgresql_where=text("class_id IS NOT NULL"),
            sqlite_where=text("class_id IS NOT NULL"),
        ),
        Index("ix_holiday_date", "date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    class_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TicketHandover(Base):
    """A ticket passed to another trainer, automatically or manually."""

    __tablename__ = "ticket_handover"
    __table_args__ = (
        CheckConstraint("from_trainer_id <> to_trainer_id", name="ck_ticket_handover_same_trainer"),
        CheckConstraint(
            "reason IN ('auto_absent', 'manual_transfer')", name="ck_ticket_handover_reason"
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name="ck_ticket_handover_status"
        ),
        Index("ix_ticket_handover_ticket", "ticket_id"),
        Index(
            "uq_ticket_handover_pending",
            "ticket_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("ix_ticket_handover_to_status", "to_trainer_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # FK lands with AI-81/AI-83 (the ``ticket`` table does not exist yet).
    ticket_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    from_trainer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    to_trainer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
