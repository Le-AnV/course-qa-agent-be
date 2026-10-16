"""Document scope: subject-wide vs class-only materials.

Revision ID: 0007_document_scope
Revises: 0006_document_class_optout
Create Date: 2026-10-16

Materials used to always belong to the whole subject (every class saw them).
A class can now also upload its own extra files, so ``document.scope`` marks
the owner: ``"course"`` (shared with every class of the subject) or ``"class"``
(private to ``document.class_id``). Every existing row stays ``"course"``.

``document.class_id`` becomes nullable here too: subject-wide materials are
uploaded from the course page and have no owning class.

Guarded: on a fresh DB ``0001_initial_schema`` (``create_all`` of the current
models) already has ``scope`` and a nullable ``class_id``, so this is a no-op
there.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0007_document_scope"
down_revision = "0006_document_class_optout"
branch_labels = None
depends_on = None


def _document_columns() -> set[str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name='document'"
    ).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    if "scope" in _document_columns():
        return
    op.alter_column("document", "class_id", existing_type=UUID(), nullable=True)
    op.add_column(
        "document",
        sa.Column(
            "scope", sa.String(length=10), nullable=False, server_default="course"
        ),
    )
    op.create_index("ix_document_scope", "document", ["scope"])


def downgrade() -> None:
    if "scope" not in _document_columns():
        return
    op.drop_index("ix_document_scope", table_name="document")
    op.drop_column("document", "scope")
    op.alter_column("document", "class_id", existing_type=UUID(), nullable=False)
