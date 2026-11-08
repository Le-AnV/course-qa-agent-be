"""Track when students last viewed ticket replies."""

import sqlalchemy as sa

from alembic import op

revision = "0038_ticket_student_seen_at"
down_revision = "0037_merge_cg_handover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ticket") as batch:
        batch.add_column(sa.Column("student_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE ticket SET student_seen_at = CURRENT_TIMESTAMP"))


def downgrade() -> None:
    with op.batch_alter_table("ticket") as batch:
        batch.drop_column("student_seen_at")
