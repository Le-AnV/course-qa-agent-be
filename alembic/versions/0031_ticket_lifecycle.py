"""Add expired ticket state.

Revision ID: 0031_ticket_lifecycle
Revises: bbf25ed25020
"""

from alembic import op

revision = "0031_ticket_lifecycle"
down_revision = "bbf25ed25020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_ticket_status", "ticket", type_="check")
    op.create_check_constraint(
        "ck_ticket_status",
        "ticket",
        "status IN ('OPEN', 'ANSWERED', 'RESOLVED', 'EXPIRED')",
    )


def downgrade() -> None:
    op.execute("UPDATE ticket SET status = 'OPEN' WHERE status = 'EXPIRED'")
    op.drop_constraint("ck_ticket_status", "ticket", type_="check")
    op.create_check_constraint(
        "ck_ticket_status",
        "ticket",
        "status IN ('OPEN', 'ANSWERED', 'RESOLVED')",
    )
