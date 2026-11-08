"""Add the passing percentage source column and backfill snapshots (AI-154).

Revision ID: 0042_quiz_passing_percentage
Revises: 0041_merge_quiz_heads
Create Date: 2026-11-08

Before this migration AI-153 stored a percentage in ``quiz.passing_score`` while
AI-154 treated the same column as points. The percentage is now the single
source of truth; ``quiz.passing_score`` remains a cached point conversion and
``quiz_attempt`` snapshots both values at grading time.
"""

import sqlalchemy as sa

from alembic import op

revision = "0042_quiz_passing_percentage"
down_revision = "0041_merge_quiz_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("quiz") as batch:
        batch.add_column(
            sa.Column(
                "passing_percentage",
                sa.Float(),
                nullable=False,
                server_default="50.0",
            )
        )
        batch.create_check_constraint(
            "ck_quiz_passing_percentage",
            "passing_percentage >= 0 AND passing_percentage <= 100",
        )

    op.add_column(
        "quiz_attempt",
        sa.Column("passing_percentage", sa.Float(), nullable=True),
    )

    # Legacy quizzes stored a percentage in passing_score (AI-153 UI labelled it
    # "Passing score (%)"). Preserve that value as the new source of truth and
    # cache its point conversion for the current question set.
    op.execute(
        sa.text(
            """
            UPDATE quiz
            SET passing_percentage = COALESCE(passing_score, 50.0),
                passing_score = COALESCE(passing_score, 50.0)
                    * COALESCE(
                        (
                            SELECT SUM(quiz_question.points)
                            FROM quiz_question
                            WHERE quiz_question.quiz_id = quiz.id
                        ),
                        0.0
                    ) / 100.0
            """
        )
    )

    # Re-snapshot already graded attempts with the quiz percentage. Attempts
    # graded before this merge may have used the old point/percentage mismatch.
    op.execute(
        sa.text(
            """
            UPDATE quiz_attempt
            SET passing_percentage = (
                    SELECT quiz.passing_percentage
                    FROM quiz
                    WHERE quiz.id = quiz_attempt.quiz_id
                ),
                passing_score = (
                    SELECT quiz.passing_percentage
                    FROM quiz
                    WHERE quiz.id = quiz_attempt.quiz_id
                ) * quiz_attempt.max_score / 100.0,
                is_passed = (
                    quiz_attempt.total_score * 100
                    >= (
                        SELECT quiz.passing_percentage
                        FROM quiz
                        WHERE quiz.id = quiz_attempt.quiz_id
                    ) * quiz_attempt.max_score
                )
            WHERE quiz_attempt.graded_at IS NOT NULL
              AND quiz_attempt.max_score IS NOT NULL
              AND quiz_attempt.max_score > 0
              AND quiz_attempt.total_score IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_column("quiz_attempt", "passing_percentage")
    with op.batch_alter_table("quiz") as batch:
        batch.drop_constraint("ck_quiz_passing_percentage", type_="check")
        batch.drop_column("passing_percentage")
