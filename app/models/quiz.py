"""SQLAlchemy models for Quiz, Questions, Options, Attempts, and Answers (AI-148)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.skill import Skill

# Portable JSON type: JSONB on PostgreSQL, JSON on SQLite
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")

QUIZ_STATUSES = ("DRAFT", "PUBLISHED", "ARCHIVED")
QUESTION_TYPES = ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK")
BLOOM_LEVELS = ("NOVICE", "BEGINNER", "COMPETENT", "PROFICIENT", "MASTER")
ATTEMPT_STATUSES = ("IN_PROGRESS", "SUBMITTED", "EXPIRED")
OPTION_LABELS = ("A", "B", "C", "D", "E", "F")


class Quiz(Base):
    """Quiz assessment metadata owned by a class and created by an instructor/admin."""

    __tablename__ = "quiz"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')",
            name="ck_quiz_status",
        ),
        CheckConstraint(
            "opens_at IS NULL OR closes_at IS NULL OR opens_at < closes_at",
            name="ck_quiz_window",
        ),
        CheckConstraint(
            "max_attempts IS NULL OR max_attempts > 0",
            name="ck_quiz_max_attempts",
        ),
        CheckConstraint(
            "passing_percentage >= 0 AND passing_percentage <= 100",
            name="ck_quiz_passing_percentage",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passing_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # AI-153/AI-154: percentage is the source of truth; ``passing_score`` above
    # is only the cached point conversion shown to students/teachers.
    passing_percentage: Mapped[float] = mapped_column(
        Float, nullable=False, default=50.0, server_default="50.0"
    )
    show_explanations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # AI-153: availability window for students (NULL = no bound) and the number
    # of attempts a student may start (NULL = unlimited).
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_attempts: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=1, server_default="1"
    )
    show_solutions_after_submission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="DRAFT", server_default="DRAFT"
    )
    # AI-151: set together when a teacher approves, cleared when the quiz is reopened.
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    questions: Mapped[list["QuizQuestion"]] = relationship(
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="QuizQuestion.order_index",
    )
    attempts: Mapped[list["QuizAttempt"]] = relationship(
        back_populates="quiz",
        cascade="all, delete-orphan",
    )


class QuizQuestion(Base):
    """Individual question within a quiz."""

    __tablename__ = "quiz_question"
    __table_args__ = (
        CheckConstraint(
            "question_type IN ('SINGLE_CHOICE', 'MULTIPLE_CHOICE', 'FILL_IN_BLANK')",
            name="ck_quiz_question_type",
        ),
        CheckConstraint(
            "difficulty IN ('NOVICE', 'BEGINNER', 'COMPETENT', 'PROFICIENT', 'MASTER', 'REMEMBER', 'UNDERSTAND', 'APPLY')",
            name="ck_quiz_question_difficulty",
        ),
        CheckConstraint("points > 0", name="ck_quiz_question_points_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("quiz.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill.id", ondelete="SET NULL"), nullable=True, index=True
    )
    question_type: Mapped[str] = mapped_column(String(30), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    points: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )
    difficulty: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NOVICE", server_default="NOVICE"
    )
    order_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    grading_config: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_flags: Mapped[list[str] | None] = mapped_column(JSON_TYPE, nullable=True)

    quiz: Mapped[Quiz] = relationship(back_populates="questions")
    skill: Mapped["Skill"] = relationship("Skill", back_populates="questions")
    options: Mapped[list["QuizOption"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="QuizOption.order_index",
    )
    answers: Mapped[list["QuizAnswer"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
    )


class QuizOption(Base):
    """Choice option (A-F) for single and multiple choice questions."""

    __tablename__ = "quiz_option"
    __table_args__ = (
        CheckConstraint(
            "option_label IN ('A', 'B', 'C', 'D', 'E', 'F')",
            name="ck_quiz_option_label",
        ),
        UniqueConstraint("question_id", "option_label", name="uq_quiz_question_option_label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("quiz_question.id", ondelete="CASCADE"), nullable=False, index=True
    )
    option_label: Mapped[str] = mapped_column(String(5), nullable=False)
    option_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    order_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    question: Mapped[QuizQuestion] = relationship(back_populates="options")


class QuizAttempt(Base):
    """Student exam session / submission attempt for a quiz."""

    __tablename__ = "quiz_attempt"
    __table_args__ = (
        CheckConstraint(
            "status IN ('IN_PROGRESS', 'SUBMITTED', 'EXPIRED')",
            name="ck_quiz_attempt_status",
        ),
        # One active attempt per student/quiz prevents a double-click or a second
        # tab from consuming two attempts (AI-153 AC-06).
        Index(
            "uq_quiz_attempt_active",
            "quiz_id",
            "student_id",
            unique=True,
            sqlite_where=text("status = 'IN_PROGRESS'"),
            postgresql_where=text("status = 'IN_PROGRESS'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("quiz.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="IN_PROGRESS", server_default="IN_PROGRESS"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passing_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passing_percentage: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skill_report: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)

    quiz: Mapped[Quiz] = relationship(back_populates="attempts")
    answers: Mapped[list["QuizAnswer"]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
    )


class QuizAnswer(Base):
    """Individual question answer recorded within a student attempt."""

    __tablename__ = "quiz_answer"
    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_quiz_attempt_question_answer"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("quiz_attempt.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("quiz_question.id", ondelete="CASCADE"), nullable=False, index=True
    )
    selected_option_labels: Mapped[list[str] | None] = mapped_column(JSON_TYPE, nullable=True)
    text_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AI-153: student-owned flag, persisted so a reload keeps the review marks.
    is_marked_for_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    score_awarded: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    breakdown: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)

    attempt: Mapped[QuizAttempt] = relationship(back_populates="answers")
    question: Mapped[QuizQuestion] = relationship(back_populates="answers")
