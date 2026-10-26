"""Store suggested follow-up questions with each assistant chat message (AI-125).

Revision ID: 0020_chat_suggested_questions
Revises: 0019_merge_chat_citations
Create Date: 2026-10-26
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_chat_suggested_questions"
down_revision = "0019_merge_chat_citations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_message",
        sa.Column("suggested_questions", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_message", "suggested_questions")
