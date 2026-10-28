"""Store thinking time and trace with each assistant chat message.

Revision ID: 0024_chat_message_thinking
Revises: 0023_merge_chat_session_pinned
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0024_chat_message_thinking"
down_revision = "0023_merge_chat_session_pinned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_message",
        sa.Column("thinking_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_message",
        sa.Column("trace", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_message", "trace")
    op.drop_column("chat_message", "thinking_ms")
