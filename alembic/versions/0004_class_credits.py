"""Add manual credits to class + hard 20-credit cap per semester (enforced in services).

Revision ID: 0004_class_credits
Revises: 0003_enrollment_pending_status
Create Date: 2026-10-15
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_class_credits"
down_revision = "0003_enrollment_pending_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info('class')").fetchall()} \
        if conn.dialect.name == "sqlite" else \
        {r[0] for r in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'class'"
        ).fetchall()}
    if "credits" not in cols:
        op.add_column("class", sa.Column("credits", sa.Integer(), nullable=False, server_default="3"))
    # Backfill any NULLs from before the default, then drop the server default
    # so the ORM default stays the source of truth.
    conn.exec_driver_sql("UPDATE \"class\" SET credits = 3 WHERE credits IS NULL")
    if conn.dialect.name == "postgresql":
        op.alter_column("class", "credits", server_default=None)
        op.create_check_constraint(
            "ck_class_credits",
            "class",
            "credits BETWEEN 1 AND 6",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        try:
            op.drop_constraint("ck_class_credits", "class", type_="check")
        except Exception as exc:  # noqa: BLE001 — best-effort downgrade
            print(f"Skipping constraint drop: {exc}")
    op.drop_column("class", "credits")
