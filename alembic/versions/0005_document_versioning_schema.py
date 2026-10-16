"""Course-shared versioning schema (plan v3 Phase 2a).

Revision ID: 0005_document_versioning_schema
Revises: 0004_document_review_note
Create Date: 2026-10-15

Evolves the existing ``document`` table in place (no parallel table, no
data move): new nullable columns + two new tables. Backfill of existing
rows lives in ``app.core.backfill_versions`` (ORM, dry-run first), not here,
so this migration stays a pure, re-runnable schema change.

Every step is guarded by an existence check: a fresh DB is built by
``0001_initial_schema`` (``Base.metadata.create_all`` of the current models),
so on such a DB the objects below already exist and this migration is a no-op.
On an older DB it adds exactly what is missing.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0005_document_versioning_schema"
down_revision = "0004_document_review_note"
branch_labels = None
depends_on = None


def _document_columns() -> set[str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name='document'"
    ).fetchall()
    return {r[0] for r in rows}


def _tables() -> set[str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ).fetchall()
    return {r[0] for r in rows}


def _indexes(table: str) -> set[str]:
    rows = op.get_bind().exec_driver_sql(
        f"SELECT indexname FROM pg_indexes WHERE tablename = '{table}'"
    ).fetchall()
    return {r[0] for r in rows}


def _constraints(table: str) -> set[str]:
    rows = op.get_bind().exec_driver_sql(
        f"SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('{table}')"
    ).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    cols = _document_columns()
    if "course_id" not in cols:
        op.add_column("document", sa.Column("course_id", UUID(), nullable=True))
        op.create_index("ix_document_course_id", "document", ["course_id"])
        op.create_foreign_key(
            "fk_document_course_id", "document", "course", ["course_id"], ["id"]
        )
    if "created_by" not in cols:
        op.add_column("document", sa.Column("created_by", UUID(), nullable=True))
        op.create_foreign_key(
            "fk_document_created_by", "document", "user", ["created_by"], ["id"]
        )
    if "name_normalized" not in cols:
        op.add_column("document", sa.Column("name_normalized", sa.String(255), nullable=True))
        op.create_index("ix_document_name_normalized", "document", ["name_normalized"])
    # App-managed pointer to document_versions.id (no DB FK: versions cascade
    # with the document row, a hard FK would fight the delete path).
    if "current_version_id" not in cols:
        op.add_column("document", sa.Column("current_version_id", UUID(), nullable=True))
    if "needs_review" not in cols:
        op.add_column(
            "document",
            sa.Column("needs_review", sa.Boolean(), nullable=False, server_default="false"),
        )
    if "deleted_at" not in cols:
        op.add_column("document", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    existing = _tables()
    if "document_versions" not in existing:
        op.create_table(
            "document_versions",
            sa.Column("id", UUID(), primary_key=True),
            sa.Column("document_id", UUID(), nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("file_hash", sa.String(128), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.Column("uploaded_by", UUID(), nullable=True),
            sa.Column("reviewed_by", UUID(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["uploaded_by"], ["user.id"]),
            sa.ForeignKeyConstraint(["reviewed_by"], ["user.id"]),
            sa.UniqueConstraint("document_id", "version_no", name="uq_version_document_no"),
        )
        op.create_index("ix_versions_document_id", "document_versions", ["document_id"])

    if "document_events" not in existing:
        op.create_table(
            "document_events",
            sa.Column("id", UUID(), primary_key=True),
            sa.Column("document_id", UUID(), nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=True),
            sa.Column("action", sa.String(30), nullable=False),
            sa.Column("actor_id", UUID(), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_events_document_id", "document_events", ["document_id"])


def downgrade() -> None:
    existing = _tables()
    if "document_events" in existing:
        op.drop_table("document_events")
    if "document_versions" in existing:
        op.drop_table("document_versions")

    # Constraint/index names differ on a DB built by ``create_all`` (Postgres
    # auto-names FKs), so drop only the ones this migration actually created.
    cols = _document_columns()
    cons = _constraints("document")
    idx = _indexes("document")
    if "deleted_at" in cols:
        op.drop_column("document", "deleted_at")
    if "needs_review" in cols:
        op.drop_column("document", "needs_review")
    if "current_version_id" in cols:
        op.drop_column("document", "current_version_id")
    if "name_normalized" in cols:
        if "ix_document_name_normalized" in idx:
            op.drop_index("ix_document_name_normalized", table_name="document")
        op.drop_column("document", "name_normalized")
    if "created_by" in cols:
        if "fk_document_created_by" in cons:
            op.drop_constraint("fk_document_created_by", "document", type_="foreignkey")
        op.drop_column("document", "created_by")
    if "course_id" in cols:
        if "fk_document_course_id" in cons:
            op.drop_constraint("fk_document_course_id", "document", type_="foreignkey")
        if "ix_document_course_id" in idx:
            op.drop_index("ix_document_course_id", table_name="document")
        op.drop_column("document", "course_id")
