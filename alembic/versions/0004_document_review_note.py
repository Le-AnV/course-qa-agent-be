"""Store reviewer note when a document is rejected.

Revision ID: 0004_document_review_note
Revises: 0003_enrollment_pending_status
Create Date: 2026-10-15

Guarded: a fresh DB is built by ``0001_initial_schema`` (``create_all`` of the
current models), where ``document.review_note`` already exists, so this is a
no-op there; older DBs gain the column.
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_document_review_note"
down_revision = "0003_enrollment_pending_status"
branch_labels = None
depends_on = None


def _document_columns() -> set[str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name='document'"
    ).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    if "review_note" not in _document_columns():
        op.add_column("document", sa.Column("review_note", sa.Text(), nullable=True))


def downgrade() -> None:
    if "review_note" in _document_columns():
        op.drop_column("document", "review_note")
