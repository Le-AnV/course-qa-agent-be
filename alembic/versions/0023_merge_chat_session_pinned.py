"""Merge chat session pinned with openai decision key pool.

Both ``0020_chat_session_pinned`` and ``0022_openai_decision_key_pool`` branch from
earlier heads. This empty merge revision keeps a single head so ``alembic upgrade head``
(and the container entrypoint) does not fail with "multiple head revisions".

Revision ID: 0023_merge_chat_session_pinned
Revises: 0020_chat_session_pinned, 0022_openai_decision_key_pool
Create Date: 2026-10-26
"""

revision = "0023_merge_chat_session_pinned"
down_revision = (
    "0020_chat_session_pinned",
    "0022_openai_decision_key_pool",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
