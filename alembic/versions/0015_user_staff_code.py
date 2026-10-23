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
    op.add_column("user", sa.Column("staff_code", sa.String(length=20), nullable=True))
    op.create_unique_constraint("uq_user_staff_code", "user", ["staff_code"])


def downgrade() -> None:
    op.drop_constraint("uq_user_staff_code", "user", type_="unique")
    op.drop_column("user", "staff_code")
