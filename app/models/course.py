"""SQLAlchemy models for Course, Class, and Enrollment."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.document import Document


class Course(Base):
    """Legacy catalog entry — decoupled from Class (class IS the subject now)."""

    __tablename__ = "course"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Class(Base):
    """A class IS the subject (mon hoc) — standalone, no course binding."""

    __tablename__ = "class"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    trainer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Manual credits entered at class creation; capped per semester at enrollment time.
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    semester: Mapped[str] = mapped_column(String(20), nullable=False)
    academic_year: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    enrollments: Mapped[list["Enrollment"]] = relationship("Enrollment", back_populates="clazz")
    documents: Mapped[list["Document"]] = relationship("Document", back_populates="clazz")
    co_trainers: Mapped[list["ClassTrainer"]] = relationship(
        "ClassTrainer", back_populates="clazz", cascade="all, delete-orphan"
    )
    config: Mapped["ClassConfig | None"] = relationship(
        "ClassConfig", back_populates="clazz", uselist=False, cascade="all, delete-orphan"
    )
    info: Mapped["ClassInfo | None"] = relationship(
        "ClassInfo", back_populates="clazz", uselist=False, cascade="all, delete-orphan"
    )


class ClassConfig(Base):
    """Typed per-class settings: roster capacity + teaching configuration.

    1:1 with ``class`` (``class_id`` is the PK) and kept in its own table so it
    can be created by ``create_all`` without altering the existing ``class``
    table. The whole row doubles as a copyable template: a teacher configures
    one class, then copies those values onto another.
    """

    __tablename__ = "class_config"

    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), primary_key=True
    )
    # Roster capacity (P5). NULL = no limit. Never below the current roster.
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Typed, constrained config values — never free-form text.
    delivery_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="on_campus")
    default_language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    answer_scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="syllabus_first"
    )
    citation_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_answer_words: Mapped[int] = mapped_column(Integer, nullable=False, default=250)
    session_length_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    clazz: Mapped["Class"] = relationship("Class", back_populates="config")


class ClassInfo(Base):
    """Teacher-authored class information (the "class page" students read).

    One row per class (``class_id`` is the PK) so ``create_all`` can add it
    without altering ``class``. Empty values mean "not filled in yet" — the
    overview hides what is empty instead of showing blank rows.
    """

    __tablename__ = "class_info"

    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), primary_key=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Online class link (Meet/Zoom/Teams) and the class chat group (Zalo/Teams).
    meeting_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    group_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Free one-liner such as "Mon & Wed 19:00-21:00, online".
    schedule_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Anything else: group registration, report schedule, class rules.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    clazz: Mapped["Class"] = relationship("Class", back_populates="info")


class ClassTrainer(Base):
    """Extra trainers on a class (co-teaching / mid-semester handover).

    ``class.trainer_id`` stays the primary trainer (ERD NOT NULL); this table
    adds the additional trainers who can also work on the class. Access checks
    read the union of both.
    """

    __tablename__ = "class_trainer"
    __table_args__ = (
        UniqueConstraint("class_id", "trainer_id", name="uq_class_trainer"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trainer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    clazz: Mapped["Class"] = relationship("Class", back_populates="co_trainers")


class Enrollment(Base):
    __tablename__ = "enrollment"
    __table_args__ = (
        UniqueConstraint("class_id", "student_id", name="uq_enrollment_class_student"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    # Trainer-marked term result: NULL (not graded) | PASSED | FAILED.
    # Only PASSED unlocks classes that require this one as a prerequisite.
    result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    clazz: Mapped["Class"] = relationship("Class", back_populates="enrollments")


class ClassPrerequisite(Base):
    """A class (subject) that must be passed before joining another class."""

    __tablename__ = "class_prerequisite"
    __table_args__ = (
        UniqueConstraint(
            "class_id", "prerequisite_class_id", name="uq_class_prerequisite"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prerequisite_class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False, index=True
    )
