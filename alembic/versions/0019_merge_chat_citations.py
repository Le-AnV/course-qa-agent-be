"""Merge the AI-63 citation column with the import_rows repair head.

Both ``0018_chat_message_citations`` and ``0018_repair_import_student_code``
branch from ``0017_repair_syllabus_unit``. This empty merge revision keeps a
single head so ``alembic upgrade head`` (and the container entrypoint) does not
fail with "multiple head revisions".

Revision ID: 0019_merge_chat_citations
Revises: 0018_chat_message_citations, 0018_repair_import_student_code
Create Date: 2026-10-24
"""

revision = "0019_merge_chat_citations"
down_revision = (
    "0018_chat_message_citations",
    "0018_repair_import_student_code",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
