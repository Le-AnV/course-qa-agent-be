"""SQLAlchemy models for Question Bank repository, questions, options, and source references (AI-160)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.course import Class
    from app.models.document import Document
    from app.models.skill import Skill
    from app.models.user import User

# Portable JSON type: JSONB on PostgreSQL, JSON on SQLite
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")

BANK_QUESTION_TYPES = ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK")
BANK_DIFFICULTY_LEVELS = (
    "NOVICE",
    "BEGINNER",
    "COMPETENT",
    "PROFICIENT",
    "MASTER",
    "REMEMBER",
    "UNDERSTAND",
    "APPLY",
)
BANK_SOURCE_TYPES = ("DOCUMENT", "CHAT_MISCONCEPTION", "MANUAL", "EXCEL_IMPORT")
BANK_STATUSES = ("DRAFT", "APPROVED", "ARCHIVED")
BANK_OPTION_LABELS = ("A", "B", "C", "D", "E", "F")


class BankQuestion(Base):
    """Question repository item scoped to a class and optionally linked to a skill."""

    __tablename__ = "bank_question"
    __table_args__ = (
        CheckConstraint(
            "question_type IN ('SINGLE_CHOICE', 'MULTIPLE_CHOICE', 'FILL_IN_BLANK')",
            name="ck_bank_question_type",
        ),
        CheckConstraint(
            "difficulty IN ('NOVICE', 'BEGINNER', 'COMPETENT', 'PROFICIENT', 'MASTER', 'REMEMBER', 'UNDERSTAND', 'APPLY')",
            name="ck_bank_question_difficulty",
        ),
        CheckConstraint(
            "source_type IN ('DOCUMENT', 'CHAT_MISCONCEPTION', 'MANUAL', 'EXCEL_IMPORT')",
            name="ck_bank_question_source_type",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'ARCHIVED')",
            name="ck_bank_question_status",
        ),
        CheckConstraint("points > 0", name="ck_bank_question_points_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )

    question_type: Mapped[str] = mapped_column(String(30), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    points: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )
    difficulty: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NOVICE", server_default="NOVICE"
    )
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="MANUAL", server_default="MANUAL"
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("document.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="APPROVED", server_default="APPROVED", index=True
    )
    grading_config: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    clazz: Mapped[Class] = relationship("Class")
    skill: Mapped[Skill | None] = relationship("Skill")
    creator: Mapped[User | None] = relationship("User")
    source_document: Mapped[Document | None] = relationship("Document")
    options: Mapped[list[BankQuestionOption]] = relationship(
        "BankQuestionOption",
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="BankQuestionOption.order_index",
    )


class BankQuestionOption(Base):
    """Choice option (A-F) for single and multiple choice bank questions."""

    __tablename__ = "bank_question_option"
    __table_args__ = (
        CheckConstraint(
            "option_label IN ('A', 'B', 'C', 'D', 'E', 'F')",
            name="ck_bank_question_option_label",
        ),
        UniqueConstraint("question_id", "option_label", name="uq_bank_question_option_label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bank_question.id", ondelete="CASCADE"), nullable=False, index=True
    )
    option_label: Mapped[str] = mapped_column(String(5), nullable=False)
    option_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    order_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    question: Mapped[BankQuestion] = relationship(back_populates="options")
