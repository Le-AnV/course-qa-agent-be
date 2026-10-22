"""Class prerequisites and trainer-marked enrollment results.

Revision ID: 0014_class_prerequisite
Revises: 0013_doc_failure_reason
Create Date: 2026-10-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0014_class_prerequisite"
down_revision = "0013_doc_failure_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("enrollment", sa.Column("result", sa.String(length=20), nullable=True))
    op.create_table(
        "class_prerequisite",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "class_id",
            sa.Uuid(),
            sa.ForeignKey("class.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "prerequisite_class_id",
            sa.Uuid(),
            sa.ForeignKey("class.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "class_id", "prerequisite_class_id", name="uq_class_prerequisite"
        ),
    )
    op.create_index("ix_class_prerequisite_class_id", "class_prerequisite", ["class_id"])
    op.create_index(
        "ix_class_prerequisite_prerequisite_class_id",
        "class_prerequisite",
        ["prerequisite_class_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_class_prerequisite_prerequisite_class_id", table_name="class_prerequisite"
    )
    op.drop_index("ix_class_prerequisite_class_id", table_name="class_prerequisite")
    op.drop_table("class_prerequisite")
    op.drop_column("enrollment", "result")
