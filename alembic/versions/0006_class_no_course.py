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
    conn = op.get_bind()
    names = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('class')"
        ).fetchall()
    }
    if "uq_class_course" in names:
        op.drop_constraint("uq_class_course", "class", type_="unique")
    cols = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'class'"
        ).fetchall()
    }
    if "course_id" in cols:
        op.drop_column("class", "course_id")


def downgrade() -> None:
    import sqlalchemy as sa

    op.add_column(
        "class",
        sa.Column("course_id", sa.Uuid(), nullable=True),
    )
    op.create_unique_constraint("uq_class_course", "class", ["course_id"])
