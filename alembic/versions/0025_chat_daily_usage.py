"""Add the per-day student chat question counter.

Revision ID: 0025_chat_daily_usage
Revises: 0024_chat_message_thinking
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0025_chat_daily_usage"
down_revision = "0024_chat_message_thinking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_daily_usage",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "usage_date", name="pk_chat_daily_usage"),
    )


def downgrade() -> None:
    op.drop_table("chat_daily_usage")
