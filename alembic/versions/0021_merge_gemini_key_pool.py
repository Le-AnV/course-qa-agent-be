"""Merge the AI-125 suggested questions column with the Gemini key pool.

Both ``0020_chat_suggested_questions`` and ``0020_gemini_key_pool`` branch from
``0019_merge_chat_citations``. This empty merge revision keeps a single head so
``alembic upgrade head`` (and the container entrypoint) does not fail with
"multiple head revisions".

Revision ID: 0021_merge_gemini_key_pool
Revises: 0020_chat_suggested_questions, 0020_gemini_key_pool
Create Date: 2026-10-26
"""

revision = "0021_merge_gemini_key_pool"
down_revision = (
    "0020_chat_suggested_questions",
    "0020_gemini_key_pool",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
