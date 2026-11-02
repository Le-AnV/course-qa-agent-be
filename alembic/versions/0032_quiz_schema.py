"""Create quiz, quiz_question, quiz_option, quiz_attempt, and quiz_answer tables (AI-148).

Revision ID: 0032_quiz_schema
Revises: 0031_ticket_lifecycle
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0032_quiz_schema"
down_revision = "0031_ticket_lifecycle"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    # 1. quiz
    op.create_table(
        "quiz",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("creator_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("time_limit_minutes", sa.Integer(), nullable=True),
        sa.Column("passing_score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="DRAFT", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')",
            name="ck_quiz_status",
        ),
        sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["creator_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quiz_class_id", "quiz", ["class_id"])
    op.create_index("ix_quiz_creator_id", "quiz", ["creator_id"])

    # 2. quiz_question
    op.create_table(
        "quiz_question",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("quiz_id", sa.Uuid(), nullable=False),
        sa.Column("question_type", sa.String(length=30), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("points", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("difficulty", sa.String(length=20), server_default="REMEMBER", nullable=False),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("rubric", sa.Text(), nullable=True),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("grading_config", JSON_TYPE, nullable=True),
        sa.CheckConstraint(
            "question_type IN ('SINGLE_CHOICE', 'MULTIPLE_CHOICE', 'FILL_IN_BLANK')",
            name="ck_quiz_question_type",
        ),
        sa.CheckConstraint(
            "difficulty IN ('REMEMBER', 'UNDERSTAND', 'APPLY')",
            name="ck_quiz_question_difficulty",
        ),
        sa.CheckConstraint("points > 0", name="ck_quiz_question_points_positive"),
        sa.ForeignKeyConstraint(["quiz_id"], ["quiz.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quiz_question_quiz_id", "quiz_question", ["quiz_id"])

    # 3. quiz_option
    op.create_table(
        "quiz_option",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("option_label", sa.String(length=5), nullable=False),
        sa.Column("option_text", sa.Text(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "option_label IN ('A', 'B', 'C', 'D', 'E', 'F')",
            name="ck_quiz_option_label",
        ),
        sa.UniqueConstraint("question_id", "option_label", name="uq_quiz_question_option_label"),
        sa.ForeignKeyConstraint(["question_id"], ["quiz_question.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quiz_option_question_id", "quiz_option", ["question_id"])

    # 4. quiz_attempt
    op.create_table(
        "quiz_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("quiz_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="IN_PROGRESS", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_score", sa.Float(), nullable=True),
        sa.Column("is_passed", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS', 'SUBMITTED', 'EXPIRED')",
            name="ck_quiz_attempt_status",
        ),
        sa.ForeignKeyConstraint(["quiz_id"], ["quiz.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quiz_attempt_quiz_id", "quiz_attempt", ["quiz_id"])
    op.create_index("ix_quiz_attempt_student_id", "quiz_attempt", ["student_id"])

    # 5. quiz_answer
    op.create_table(
        "quiz_answer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("selected_option_labels", JSON_TYPE, nullable=True),
        sa.Column("text_answer", sa.Text(), nullable=True),
        sa.Column("score_awarded", sa.Float(), nullable=True),
        sa.Column("breakdown", JSON_TYPE, nullable=True),
        sa.UniqueConstraint("attempt_id", "question_id", name="uq_quiz_attempt_question_answer"),
        sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempt.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["quiz_question.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quiz_answer_attempt_id", "quiz_answer", ["attempt_id"])
    op.create_index("ix_quiz_answer_question_id", "quiz_answer", ["question_id"])


def downgrade() -> None:
    op.drop_table("quiz_answer")
    op.drop_table("quiz_attempt")
    op.drop_table("quiz_option")
    op.drop_table("quiz_question")
    op.drop_table("quiz")
