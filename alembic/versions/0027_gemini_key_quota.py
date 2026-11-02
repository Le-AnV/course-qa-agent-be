"""Add daily quota tracking to gemini_api_key.

Revision ID: 0027_gemini_key_quota
Revises: 0026_merge_chat_quota_context
Create Date: 2026-10-28
"""

import sqlalchemy as sa

from alembic import op

revision = "0027_gemini_key_quota"
down_revision = "0026_merge_chat_quota_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {col["name"] for col in inspector.get_columns("gemini_api_key")}
    if "daily_requests" not in existing_cols:
        op.add_column(
            "gemini_api_key",
            sa.Column("daily_requests", sa.Integer(), nullable=False, server_default="0"),
        )
    if "max_daily_requests" not in existing_cols:
        op.add_column(
            "gemini_api_key",
            sa.Column("max_daily_requests", sa.Integer(), nullable=False, server_default="100"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {col["name"] for col in inspector.get_columns("gemini_api_key")}
    if "max_daily_requests" in existing_cols:
        op.drop_column("gemini_api_key", "max_daily_requests")
    if "daily_requests" in existing_cols:
        op.drop_column("gemini_api_key", "daily_requests")
