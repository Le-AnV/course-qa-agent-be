"""Per-class document opt-out (AI-37).

Revision ID: 0006_document_class_optout
Revises: 0005_document_versioning_schema
Create Date: 2026-10-16

Adds ``document_class_optout``: a class can remove a document from its own
list without affecting the rest of the subject. The document keeps belonging
to the course, so every other class still sees it; deleting the row restores
it. No data backfill needed (empty table on create).

Guarded: on a fresh DB ``0001_initial_schema`` (``create_all`` of the current
models) already creates the table, so this is a no-op there.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0006_document_class_optout"
down_revision = "0005_document_versioning_schema"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    if "document_class_optout" in _tables():
        return
    op.create_table(
        "document_class_optout",
        sa.Column("document_id", UUID(), nullable=False),
        sa.Column("class_id", UUID(), nullable=False),
        sa.Column("removed_by", UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["removed_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("document_id", "class_id", name="pk_document_class_optout"),
    )


def downgrade() -> None:
    if "document_class_optout" in _tables():
        op.drop_table("document_class_optout")
