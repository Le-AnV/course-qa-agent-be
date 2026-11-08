"""Add show_solutions_after_submission to quiz table.

Revision ID: 0043_quiz_show_solutions
Revises: 0042_question_bank
Create Date: 2026-11-08
"""

import sqlalchemy as sa

from alembic import op

revision = "0043_quiz_show_solutions"
down_revision = "0042_question_bank"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    table_names = insp.get_table_names()
    if "quiz" in table_names:
        quiz_cols = [c["name"] for c in insp.get_columns("quiz")]
        if "show_solutions_after_submission" not in quiz_cols:
            with op.batch_alter_table("quiz") as batch:
                batch.add_column(
                    sa.Column(
                        "show_solutions_after_submission",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.true(),
                    )
                )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    table_names = insp.get_table_names()
    if "quiz" in table_names:
        quiz_cols = [c["name"] for c in insp.get_columns("quiz")]
        if "show_solutions_after_submission" in quiz_cols:
            with op.batch_alter_table("quiz") as batch:
                batch.drop_column("show_solutions_after_submission")
