"""Add class_info: the teacher-authored class page students read.

One row per class (``class_id`` is the primary key), kept in its own table so
the existing ``class`` table needs no ALTER. Empty values mean "not filled in
yet" — the overview hides what is empty.

Revision ID: 0010_class_info
Revises: 0009_class_config
Create Date: 2026-10-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_class_info"
down_revision = "0009_class_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "class_info",
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("meeting_url", sa.String(length=500), nullable=True),
        sa.Column("group_url", sa.String(length=500), nullable=True),
        sa.Column("schedule_note", sa.String(length=500), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("class_id"),
    )


def downgrade() -> None:
    op.drop_table("class_info")
