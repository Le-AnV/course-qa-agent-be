"""Employee ID (MSNV) in bulk-import row reports.

Revision ID: 0016_import_row_staff_code
Revises: 0015_user_staff_code
Create Date: 2026-10-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0016_import_row_staff_code"
down_revision = "0015_user_staff_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_rows", sa.Column("staff_code", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("import_rows", "staff_code")
