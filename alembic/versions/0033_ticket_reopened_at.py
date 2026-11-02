"""Track the latest reopen so expired windows restart after reopening (AI-89).

Revision ID: 0033_ticket_reopened_at
Revises: 0032_quiz_schema
"""

import sqlalchemy as sa

from alembic import op

revision = "0033_ticket_reopened_at"
down_revision = "0032_quiz_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket", sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket", "reopened_at")
