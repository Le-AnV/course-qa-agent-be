"""Add persistent local path to documents.

Revision ID: 0008_document_storage
Revises: 0007_student_code
Create Date: 2026-10-16
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_document_storage"
down_revision = "0007_student_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("document")}
    if "storage_path" not in columns:
        op.add_column("document", sa.Column("storage_path", sa.String(500), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("document")}
    if "storage_path" in columns:
        op.drop_column("document", "storage_path")
