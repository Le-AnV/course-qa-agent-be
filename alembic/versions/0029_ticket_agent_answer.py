"""Preserve the selected agent answer separately from the student's question.

Revision ID: 0029_ticket_agent_answer
Revises: 0028_student_tickets
"""

import sqlalchemy as sa

from alembic import op

revision = "0029_ticket_agent_answer"
down_revision = "0028_student_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket", sa.Column("agent_answer", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket", "agent_answer")
