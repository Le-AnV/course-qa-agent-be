"""Add student_code (MSSV) to user + import_rows.

Revision ID: 0007_student_code
Revises: 0006_class_no_course
Create Date: 2026-10-15
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_student_code"
down_revision = "0006_class_no_course"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    user_cols = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'user'"
        ).fetchall()
    }
    if "student_code" not in user_cols:
        op.add_column("user", sa.Column("student_code", sa.String(20), nullable=True))

    user_cons = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('\"user\"')"
        ).fetchall()
    }
    if "uq_user_student_code" not in user_cons:
        op.create_unique_constraint("uq_user_student_code", "user", ["student_code"])

    import_cols = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'import_rows'"
        ).fetchall()
    }
    if "student_code" not in import_cols:
        op.add_column("import_rows", sa.Column("student_code", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("import_rows", "student_code")
    op.drop_constraint("uq_user_student_code", "user", type_="unique")
    op.drop_column("user", "student_code")
