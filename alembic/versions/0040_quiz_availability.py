"""Add quiz availability window, attempt limit and review marks (AI-153)."""

import sqlalchemy as sa

from alembic import op

revision = "0040_quiz_availability"
down_revision = "0039_quiz_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("quiz") as batch:
        batch.add_column(sa.Column("opens_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("closes_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("max_attempts", sa.Integer(), nullable=True, server_default="1")
        )
        batch.create_check_constraint(
            "ck_quiz_window",
            "opens_at IS NULL OR closes_at IS NULL OR opens_at < closes_at",
        )
        batch.create_check_constraint(
            "ck_quiz_max_attempts",
            "max_attempts IS NULL OR max_attempts > 0",
        )

    with op.batch_alter_table("quiz_answer") as batch:
        batch.add_column(
            sa.Column(
                "is_marked_for_review",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_index(
        "uq_quiz_attempt_active",
        "quiz_attempt",
        ["quiz_id", "student_id"],
        unique=True,
        sqlite_where=sa.text("status = 'IN_PROGRESS'"),
        postgresql_where=sa.text("status = 'IN_PROGRESS'"),
    )


def downgrade() -> None:
    op.drop_index("uq_quiz_attempt_active", table_name="quiz_attempt")

    with op.batch_alter_table("quiz_answer") as batch:
        batch.drop_column("is_marked_for_review")

    with op.batch_alter_table("quiz") as batch:
        batch.drop_constraint("ck_quiz_max_attempts", type_="check")
        batch.drop_constraint("ck_quiz_window", type_="check")
        batch.drop_column("max_attempts")
        batch.drop_column("closes_at")
        batch.drop_column("opens_at")
