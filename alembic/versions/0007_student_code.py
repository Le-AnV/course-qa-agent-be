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
    op.add_column("user", sa.Column("student_code", sa.String(20), nullable=True))
    op.create_unique_constraint("uq_user_student_code", "user", ["student_code"])
    op.add_column("import_rows", sa.Column("student_code", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("import_rows", "student_code")
    op.drop_constraint("uq_user_student_code", "user", type_="unique")
    op.drop_column("user", "student_code")
