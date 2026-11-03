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
    op.add_column("ticket", sa.Column("escalated_to_id", sa.Uuid(), nullable=True))
    op.add_column(
        "ticket", sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_ticket_escalated_to_id_user",
        "ticket",
        "user",
        ["escalated_to_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_ticket_escalated_to_id", "ticket", ["escalated_to_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_escalated_to_id", table_name="ticket")
    op.drop_constraint("fk_ticket_escalated_to_id_user", "ticket", type_="foreignkey")
    op.drop_column("ticket", "escalated_at")
    op.drop_column("ticket", "escalated_to_id")
