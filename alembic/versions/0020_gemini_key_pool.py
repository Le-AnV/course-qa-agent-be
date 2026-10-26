"""Admin-managed Gemini API key pool.

Revision ID: 0020_gemini_key_pool
Revises: 0019_merge_chat_citations
Create Date: 2026-10-26
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_gemini_key_pool"
down_revision = "0019_merge_chat_citations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gemini_api_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("account_label", sa.String(length=255), nullable=True),
        sa.Column("project_label", sa.String(length=255), nullable=True),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.String(length=8), nullable=False),
        sa.Column("key_last4", sa.String(length=8), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "available_models",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_gemini_api_key_name"),
        sa.UniqueConstraint("key_fingerprint", name="uq_gemini_api_key_fingerprint"),
    )
    op.create_index("ix_gemini_api_key_status", "gemini_api_key", ["status"])
    op.create_index(
        "ix_gemini_api_key_key_fingerprint",
        "gemini_api_key",
        ["key_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index("ix_gemini_api_key_key_fingerprint", table_name="gemini_api_key")
    op.drop_index("ix_gemini_api_key_status", table_name="gemini_api_key")
    op.drop_table("gemini_api_key")
