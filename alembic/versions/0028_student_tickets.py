"""Create student-initiated tickets and replies (AI-81).

Revision ID: 0028_student_tickets
Revises: 0027_gemini_key_quota
Create Date: 2026-10-29
"""

import sqlalchemy as sa

from alembic import op

revision = "0028_student_tickets"
down_revision = "0027_gemini_key_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="OPEN", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ANSWERED', 'RESOLVED')",
            name="ck_ticket_status",
        ),
        sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["student_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_class_id", "ticket", ["class_id"])
    op.create_index("ix_ticket_student_id", "ticket", ["student_id"])
    op.create_index("ix_ticket_created_at", "ticket", ["created_at"])

    op.create_table(
        "ticket_reply",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["ticket.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_reply_ticket_id", "ticket_reply", ["ticket_id"])
    op.create_index("ix_ticket_reply_user_id", "ticket_reply", ["user_id"])
    op.create_index("ix_ticket_reply_created_at", "ticket_reply", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ticket_reply_created_at", table_name="ticket_reply")
    op.drop_index("ix_ticket_reply_user_id", table_name="ticket_reply")
    op.drop_index("ix_ticket_reply_ticket_id", table_name="ticket_reply")
    op.drop_table("ticket_reply")
    op.drop_index("ix_ticket_created_at", table_name="ticket")
    op.drop_index("ix_ticket_student_id", table_name="ticket")
    op.drop_index("ix_ticket_class_id", table_name="ticket")
    op.drop_table("ticket")
