"""Allow PENDING enrollments (student request awaits TA approval).

Revision ID: 0003_enrollment_pending_status
Revises: 0002_document_schema_align
Create Date: 2026-10-15
"""

from alembic import op

revision = "0003_enrollment_pending_status"
down_revision = "0002_document_schema_align"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    names = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('enrollment')"
        ).fetchall()
    }
    if "ck_enrollment_status" in names:
        op.drop_constraint("ck_enrollment_status", "enrollment", type_="check")
    op.create_check_constraint(
        "ck_enrollment_status",
        "enrollment",
        "status IN ('ACTIVE', 'INACTIVE', 'PENDING')",
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("UPDATE enrollment SET status = 'INACTIVE' WHERE status = 'PENDING'")
    op.drop_constraint("ck_enrollment_status", "enrollment", type_="check")
    op.create_check_constraint(
        "ck_enrollment_status",
        "enrollment",
        "status IN ('ACTIVE', 'INACTIVE')",
    )
