"""Decouple class from course: class IS the subject (no course binding).

Revision ID: 0006_class_no_course
Revises: 0005_one_class_per_course
Create Date: 2026-10-15
"""

from alembic import op

revision = "0006_class_no_course"
down_revision = "0005_one_class_per_course"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_class_course", "class", type_="unique")
    op.drop_column("class", "course_id")


def downgrade() -> None:
    import sqlalchemy as sa

    op.add_column(
        "class",
        sa.Column("course_id", sa.Uuid(), nullable=True),
    )
    op.create_unique_constraint("uq_class_course", "class", ["course_id"])
