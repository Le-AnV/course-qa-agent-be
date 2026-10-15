"""Align document table with E2 service model (AI-35/37/41).

Revision ID: 0002_document_schema_align
Revises: 0001_initial_schema
Create Date: 2026-10-15

Old local DBs (created by early Base.metadata.create_all) have:
  file_size, file_path, approval_status (PENDING/APPROVED/REJECTED),
  processing_status (NOT_STARTED/PROCESSING/DONE/FAILED),
  file_type UPPERCASE without dot.

New model expects:
  size_bytes, mime_type, status
  (pending/processing/indexed/rejected/failed),
  file_type lowercase with dot (.pdf ...).

Data migration preserves rows:
  size_bytes <- file_size
  status <- mapped from (approval_status, processing_status)
  file_type <- lowercased with dot
  mime_type <- derived from file_type
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_document_schema_align"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
}


def upgrade() -> None:
    conn = op.get_bind()
    cols = {r[0] for r in conn.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name='document'"
    ).fetchall()}
    if "document" not in {r[0] for r in conn.exec_driver_sql(
        "SELECT table_name FROM information_schema.tables WHERE table_name='document'"
    ).fetchall()}:
        return

    if "size_bytes" not in cols:
        op.add_column("document", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    if "mime_type" not in cols:
        op.add_column("document", sa.Column("mime_type", sa.String(255), nullable=True))
    if "status" not in cols:
        op.add_column("document", sa.Column("status", sa.String(20), nullable=True))

    # Backfill when legacy columns exist.
    if {"file_size", "approval_status", "processing_status", "file_type"} <= cols:
        conn.exec_driver_sql("""
            UPDATE document SET
              size_bytes = COALESCE(size_bytes, file_size, 0),
              status = COALESCE(status,
                CASE
                  WHEN approval_status = 'REJECTED' THEN 'rejected'
                  WHEN processing_status = 'DONE' THEN 'indexed'
                  WHEN processing_status = 'PROCESSING' THEN 'processing'
                  WHEN processing_status = 'FAILED' THEN 'failed'
                  ELSE 'pending'
                END),
              file_type = CASE
                WHEN file_type LIKE '.%' THEN lower(file_type)
                WHEN lower(file_type) = 'pdf' THEN '.pdf'
                WHEN lower(file_type) IN ('docx') THEN '.docx'
                WHEN lower(file_type) = 'md' THEN '.md'
                WHEN lower(file_type) = 'txt' THEN '.txt'
                WHEN lower(file_type) IN ('ppt', 'pptx') THEN '.pptx'
                WHEN lower(file_type) IN ('xls', 'xlsx') THEN '.xlsx'
                ELSE lower('.' || file_type)
              END
        """)
        for ext, mime in MIME_BY_EXT.items():
            conn.execute(
                sa.text("UPDATE document SET mime_type = :mime WHERE file_type = :ext AND mime_type IS NULL"),
                {"mime": mime, "ext": ext},
            )
        conn.exec_driver_sql(
            "UPDATE document SET mime_type = COALESCE(mime_type, 'application/octet-stream')"
        )

    # Enforce NOT NULL + defaults for the new model.
    conn.exec_driver_sql("UPDATE document SET size_bytes = 0 WHERE size_bytes IS NULL")
    conn.exec_driver_sql("UPDATE document SET status = 'pending' WHERE status IS NULL")
    conn.exec_driver_sql(
        "UPDATE document SET mime_type = 'application/octet-stream' WHERE mime_type IS NULL"
    )
    with op.batch_alter_table("document") as batch:
        batch.alter_column("size_bytes", existing_type=sa.BigInteger(), nullable=False, server_default="0")
        batch.alter_column("mime_type", existing_type=sa.String(255), nullable=False, server_default="application/octet-stream")
        batch.alter_column("status", existing_type=sa.String(20), nullable=False, server_default="pending")

    # Drop legacy columns/constraints only if they exist.
    for constraint in (
        "ck_document_approval_status",
        "ck_document_processing_status",
        "ck_document_file_type",
        "document_file_size_check",
    ):
        try:
            op.drop_constraint(constraint, "document", type_="check")
        except Exception:
            pass
    for legacy in ("file_path", "file_size", "approval_status", "processing_status"):
        if legacy in cols:
            try:
                op.drop_column("document", legacy)
            except Exception:
                pass


def downgrade() -> None:
    # One-way data migration: keep new columns on downgrade.
    pass
