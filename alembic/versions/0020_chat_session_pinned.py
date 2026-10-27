"""Add is_pinned and pinned_at to chat_session for Pin Chat feature.

Revision ID: 0020_chat_session_pinned
Revises: 0019_merge_chat_citations
Create Date: 2026-10-26
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_chat_session_pinned"
down_revision = "0019_merge_chat_citations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_session",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "chat_session",
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_chat_session_is_pinned", "chat_session", ["is_pinned"])


def downgrade() -> None:
    op.drop_index("ix_chat_session_is_pinned", table_name="chat_session")
    op.drop_column("chat_session", "pinned_at")
    op.drop_column("chat_session", "is_pinned")
