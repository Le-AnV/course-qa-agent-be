"""Add class_config: roster capacity + typed teaching configuration.

One row per class (``class_id`` is the primary key). Kept in its own table so
the existing ``class`` table needs no ALTER, and so the values can be copied
from one class onto another as a reusable template.

Revision ID: 0009_class_config
Revises: 0008_class_trainer
Create Date: 2026-10-18
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_class_config"
down_revision = "0008_class_trainer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "class_config",
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("delivery_mode", sa.String(length=20), nullable=False, server_default="on_campus"),
        sa.Column("default_language", sa.String(length=10), nullable=False, server_default="en"),
        sa.Column("answer_scope", sa.String(length=20), nullable=False, server_default="syllabus_first"),
        sa.Column("citation_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_answer_words", sa.Integer(), nullable=False, server_default="250"),
        sa.Column("session_length_minutes", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("class_id"),
    )


def downgrade() -> None:
    op.drop_table("class_config")
