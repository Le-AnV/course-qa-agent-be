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
    conn = op.get_bind()
    cols = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'class'"
        ).fetchall()
    }
    if "course_id" in cols:
        op.create_unique_constraint("uq_class_course", "class", ["course_id"])


def downgrade() -> None:
    conn = op.get_bind()
    names = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('class')"
        ).fetchall()
    }
    if "uq_class_course" in names:
        op.drop_constraint("uq_class_course", "class", type_="unique")
