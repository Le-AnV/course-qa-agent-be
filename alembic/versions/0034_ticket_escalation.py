"""Add ticket escalation fields.

Revision ID: 0034_ticket_escalation
Revises: 0033_ticket_reopened_at
"""

import sqlalchemy as sa

from alembic import op

revision = "0034_ticket_escalation"
down_revision = "0033_ticket_reopened_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ticket") as batch:
        batch.add_column(sa.Column("escalated_to_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_foreign_key(
            "fk_ticket_escalated_to_id_user",
            "user",
            ["escalated_to_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_ticket_escalated_to_id", ["escalated_to_id"])


def downgrade() -> None:
    with op.batch_alter_table("ticket") as batch:
        batch.drop_index("ix_ticket_escalated_to_id")
        batch.drop_constraint("fk_ticket_escalated_to_id_user", type_="foreignkey")
        batch.drop_column("escalated_at")
        batch.drop_column("escalated_to_id")
