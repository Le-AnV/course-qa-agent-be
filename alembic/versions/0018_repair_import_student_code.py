"""Restore import_rows.student_code on databases with schema drift.

Revision ID: 0018_repair_import_student_code
Revises: 0017_repair_syllabus_unit
Create Date: 2026-10-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_repair_import_student_code"
down_revision = "0017_repair_syllabus_unit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("import_rows"):
        return

    columns = {column["name"] for column in inspector.get_columns("import_rows")}
    if "student_code" not in columns:
        op.add_column(
            "import_rows",
            sa.Column("student_code", sa.String(length=20), nullable=True),
        )


def downgrade() -> None:
    # No-op: on a fresh database the column is owned by 0007_student_code.
    pass
