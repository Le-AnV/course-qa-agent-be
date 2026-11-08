"""Add quiz grading and result fields (AI-154).

Revision ID: 0039_quiz_grading_results
Revises: 0039_quiz_approval
Create Date: 2026-11-05

The fields are intentionally limited to AI-154's grading/result ownership:
``quiz.show_explanations`` and the attempt/answer score snapshot fields.
Quiz access-window fields (open/close/max attempts) belong to AI-153 and are
added by that story's migration.
"""

import sqlalchemy as sa

from alembic import op

# Keep the revision id <= 32 chars: alembic_version.version_num is varchar(32).
revision = "0039_quiz_grading_results"
down_revision = "0039_quiz_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quiz",
        sa.Column(
            "show_explanations",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column("quiz_attempt", sa.Column("max_score", sa.Float(), nullable=True))
    op.add_column("quiz_attempt", sa.Column("passing_score", sa.Float(), nullable=True))
    op.add_column(
        "quiz_attempt",
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("quiz_answer", sa.Column("is_correct", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("quiz_answer", "is_correct")
    op.drop_column("quiz_attempt", "graded_at")
    op.drop_column("quiz_attempt", "passing_score")
    op.drop_column("quiz_attempt", "max_score")
    op.drop_column("quiz", "show_explanations")
