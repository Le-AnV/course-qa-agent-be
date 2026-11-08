"""Create bank_question and bank_question_option tables for Question Bank repository (AI-160).

Revision ID: 0042_question_bank
Revises: 0041_skill_taxonomy
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0042_question_bank"
down_revision = "0041_skill_taxonomy"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    # 0. Safeguard: ensure quiz availability columns exist if earlier migration was stamped or skipped
    bind = op.get_bind()
    insp = sa.inspect(bind)
    table_names = insp.get_table_names()
    if "quiz" in table_names:
        quiz_cols = [c["name"] for c in insp.get_columns("quiz")]
        with op.batch_alter_table("quiz") as batch:
            if "opens_at" not in quiz_cols:
                batch.add_column(sa.Column("opens_at", sa.DateTime(timezone=True), nullable=True))
            if "closes_at" not in quiz_cols:
                batch.add_column(sa.Column("closes_at", sa.DateTime(timezone=True), nullable=True))
            if "max_attempts" not in quiz_cols:
                batch.add_column(
                    sa.Column("max_attempts", sa.Integer(), nullable=True, server_default="1")
                )

    if "quiz_answer" in table_names:
        answer_cols = [c["name"] for c in insp.get_columns("quiz_answer")]
        if "is_marked_for_review" not in answer_cols:
            with op.batch_alter_table("quiz_answer") as batch:
                batch.add_column(
                    sa.Column(
                        "is_marked_for_review",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.false(),
                    )
                )

    # 1. bank_question
    op.create_table(
        "bank_question",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("question_type", sa.String(length=30), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("points", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("difficulty", sa.String(length=20), server_default="NOVICE", nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("rubric", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=30), server_default="MANUAL", nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="APPROVED", nullable=False),
        sa.Column("grading_config", JSON_TYPE, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "question_type IN ('SINGLE_CHOICE', 'MULTIPLE_CHOICE', 'FILL_IN_BLANK')",
            name="ck_bank_question_type",
        ),
        sa.CheckConstraint(
            "difficulty IN ('NOVICE', 'BEGINNER', 'COMPETENT', 'PROFICIENT', 'MASTER', 'REMEMBER', 'UNDERSTAND', 'APPLY')",
            name="ck_bank_question_difficulty",
        ),
        sa.CheckConstraint(
            "source_type IN ('DOCUMENT', 'CHAT_MISCONCEPTION', 'MANUAL', 'EXCEL_IMPORT')",
            name="ck_bank_question_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'ARCHIVED')",
            name="ck_bank_question_status",
        ),
        sa.CheckConstraint("points > 0", name="ck_bank_question_points_positive"),
        sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skill.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_document_id"], ["document.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bank_question_class_id", "bank_question", ["class_id"])
    op.create_index("ix_bank_question_skill_id", "bank_question", ["skill_id"])
    op.create_index("ix_bank_question_created_by_id", "bank_question", ["created_by_id"])
    op.create_index("ix_bank_question_source_doc_id", "bank_question", ["source_document_id"])
    op.create_index("ix_bank_question_status", "bank_question", ["status"])

    # 2. bank_question_option
    op.create_table(
        "bank_question_option",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("option_label", sa.String(length=5), nullable=False),
        sa.Column("option_text", sa.Text(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "option_label IN ('A', 'B', 'C', 'D', 'E', 'F')",
            name="ck_bank_question_option_label",
        ),
        sa.UniqueConstraint("question_id", "option_label", name="uq_bank_question_option_label"),
        sa.ForeignKeyConstraint(["question_id"], ["bank_question.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bank_question_option_qid", "bank_question_option", ["question_id"])


def downgrade() -> None:
    op.drop_table("bank_question_option")
    op.drop_table("bank_question")
