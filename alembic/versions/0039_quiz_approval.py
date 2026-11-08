"""Record who approved a quiz and when (AI-151)."""

import sqlalchemy as sa

from alembic import op

revision = "0039_quiz_approval"
down_revision = "0038_ticket_student_seen_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("quiz") as batch:
        batch.add_column(sa.Column("approved_by_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_quiz_approved_by_id_user",
            "user",
            ["approved_by_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index("ix_quiz_approved_by", "quiz", ["approved_by_id"])


def downgrade() -> None:
    op.drop_index("ix_quiz_approved_by", table_name="quiz")
    with op.batch_alter_table("quiz") as batch:
        batch.drop_constraint("fk_quiz_approved_by_id_user", type_="foreignkey")
        batch.drop_column("approved_at")
        batch.drop_column("approved_by_id")
