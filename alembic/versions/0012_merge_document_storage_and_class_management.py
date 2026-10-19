"""Merge the document storage and class management migration branches.

Revision ID: 0012_merge_doc_class
Revises: 0008_document_storage, 0011_document_version
Create Date: 2026-10-19
"""

revision = "0012_merge_doc_class"
down_revision = ("0008_document_storage", "0011_document_version")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
