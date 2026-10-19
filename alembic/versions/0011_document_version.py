"""Add document.version for replace-by-name versioning (AI-39 AC-03).

Revision ID: 0011_document_version
Revises: 0010_class_info
Create Date: 2026-10-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_document_version"
down_revision = "0010_class_info"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info('document')").fetchall()}
    else:
        cols = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'document'"
            ).fetchall()
        }
    if "version" not in cols:
        op.add_column(
            "document",
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
    # Existing rows are version 1 (backfill any NULLs from before the default),
    # then drop the server default so the ORM default stays the source of truth.
    conn.exec_driver_sql("UPDATE document SET version = 1 WHERE version IS NULL")
    if conn.dialect.name == "postgresql":
        op.alter_column("document", "version", server_default=None)


def downgrade() -> None:
    op.drop_column("document", "version")
