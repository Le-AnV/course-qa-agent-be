"""Employee ID (MSNV) for trainer/TA/admin accounts.

Revision ID: 0015_user_staff_code
Revises: 0015_syllabus_roadmap
Create Date: 2026-10-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_user_staff_code"
down_revision = "0015_syllabus_roadmap"
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
    if "staff_code" not in user_cols:
        op.add_column("user", sa.Column("staff_code", sa.String(length=20), nullable=True))

    user_cons = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('\"user\"')"
        ).fetchall()
    }
    if "uq_user_staff_code" not in user_cons:
        op.create_unique_constraint("uq_user_staff_code", "user", ["staff_code"])


def downgrade() -> None:
    op.drop_constraint("uq_user_staff_code", "user", type_="unique")
    op.drop_column("user", "staff_code")
