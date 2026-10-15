"""Enforce one class per course (1 mon 1 lop).

Revision ID: 0005_one_class_per_course
Revises: 0004_class_credits
Create Date: 2026-10-15
"""

from alembic import op

revision = "0005_one_class_per_course"
down_revision = "0004_class_credits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_class_course", "class", ["course_id"])


def downgrade() -> None:
    op.drop_constraint("uq_class_course", "class", type_="unique")
