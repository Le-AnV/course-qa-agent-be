"""Persist the student's original chat question on teacher tickets.

Revision ID: 0030_ticket_original_question
Revises: 0029_ticket_agent_answer
"""

import sqlalchemy as sa

from alembic import op

revision = "0030_ticket_original_question"
down_revision = "0029_ticket_agent_answer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket", sa.Column("original_question", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket", "original_question")
