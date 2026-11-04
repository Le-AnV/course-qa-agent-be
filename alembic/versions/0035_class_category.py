"""Create class_category table for 2-level content gap classification (AI-91, Epic E6).

Revision ID: 0035_class_category
Revises: 0034_ticket_escalation
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_class_category"
down_revision = "0034_ticket_escalation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "class_category",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=50), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["class_category.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_class_category_class_id", "class_category", ["class_id"])
    op.create_index("ix_class_category_parent_id", "class_category", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_class_category_parent_id", table_name="class_category")
    op.drop_index("ix_class_category_class_id", table_name="class_category")
    op.drop_table("class_category")
