"""Merge the chat quota/thinking migrations with the AI-63 context exclusion.

Both ``0025_chat_daily_usage`` (which descends from
``0024_chat_message_thinking``) and ``0024_chat_context_exclusion`` branch from
``0023_merge_chat_session_pinned``. This empty merge revision keeps a single
head so ``alembic upgrade head`` does not fail with multiple head revisions.

Revision ID: 0026_merge_chat_quota_context
Revises: 0025_chat_daily_usage, 0024_chat_context_exclusion
Create Date: 2026-10-27
"""

revision = "0026_merge_chat_quota_context"
down_revision = (
    "0025_chat_daily_usage",
    "0024_chat_context_exclusion",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
