"""Syllabus roadmap units extracted from syllabus markdown (AI-133).

Revision ID: 0015_syllabus_roadmap
Revises: 0014_class_prerequisite
Create Date: 2026-10-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_syllabus_roadmap"
down_revision = "0014_class_prerequisite"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "syllabus_unit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "class_id",
            sa.Uuid(),
            sa.ForeignKey("class.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("unit_number", sa.Integer(), nullable=False),
        sa.Column("unit_title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("topic_outline", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "class_id", "unit_number", name="uq_syllabus_unit_class_number"
        ),
    )
    op.create_index("ix_syllabus_unit_class_id", "syllabus_unit", ["class_id"])
    op.create_index("ix_syllabus_unit_document_id", "syllabus_unit", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_syllabus_unit_document_id", table_name="syllabus_unit")
    op.drop_index("ix_syllabus_unit_class_id", table_name="syllabus_unit")
    op.drop_table("syllabus_unit")

