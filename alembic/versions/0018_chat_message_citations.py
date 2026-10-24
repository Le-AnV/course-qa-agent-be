"""Store citations with each assistant chat message (AI-63 AC-03).

Revision ID: 0018_chat_message_citations
Revises: 0017_repair_syllabus_unit
Create Date: 2026-10-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_chat_message_citations"
down_revision = "0017_repair_syllabus_unit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_message",
        sa.Column("citations", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_message", "citations")
