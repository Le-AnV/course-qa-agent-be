"""Store a trainer-facing document processing failure reason.

Revision ID: 0013_doc_failure_reason
Revises: 0012_merge_doc_class
Create Date: 2026-10-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0013_doc_failure_reason"
down_revision = "0012_merge_doc_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document", sa.Column("failure_reason", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    op.drop_column("document", "failure_reason")
