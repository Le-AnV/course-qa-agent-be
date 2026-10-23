"""Ensure syllabus_unit exists on databases stamped before the AI-133 merge.

Revision ID: 0017_repair_syllabus_unit
Revises: 0016_import_row_staff_code
Create Date: 2026-10-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0017_repair_syllabus_unit"
down_revision = "0016_import_row_staff_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("syllabus_unit"):
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

    indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("syllabus_unit")
    }
    if "ix_syllabus_unit_class_id" not in indexes:
        op.create_index("ix_syllabus_unit_class_id", "syllabus_unit", ["class_id"])
    if "ix_syllabus_unit_document_id" not in indexes:
        op.create_index(
            "ix_syllabus_unit_document_id", "syllabus_unit", ["document_id"]
        )


def downgrade() -> None:
    # No-op: on a fresh database the table is owned by 0015_syllabus_roadmap.
    pass
