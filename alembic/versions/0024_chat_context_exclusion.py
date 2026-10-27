"""Keep refused turns out of later chat context (AI-63).

Blocked turns stay visible when a conversation is reopened, but they must not
be replayed as conversational context for later rewrites or answers.

Revision ID: 0024_chat_context_exclusion
Revises: 0023_merge_chat_session_pinned
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0024_chat_context_exclusion"
down_revision = "0023_merge_chat_session_pinned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_message",
        sa.Column(
            "excluded_from_context",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_message", "excluded_from_context")
