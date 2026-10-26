"""Admin-managed OpenAI key pool for the Decisions scope gate.

Revision ID: 0022_openai_decision_key_pool
Revises: 0021_merge_gemini_key_pool
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0022_openai_decision_key_pool"
down_revision = "0021_merge_gemini_key_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "openai_api_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.String(length=8), nullable=False),
        sa.Column("key_last4", sa.String(length=8), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=64), nullable=False),
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
        sa.UniqueConstraint("name", name="uq_openai_api_key_name"),
        sa.UniqueConstraint("key_fingerprint", name="uq_openai_api_key_fingerprint"),
    )
    op.create_index("ix_openai_api_key_status", "openai_api_key", ["status"])
    op.create_index(
        "ix_openai_api_key_key_fingerprint",
        "openai_api_key",
        ["key_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index("ix_openai_api_key_key_fingerprint", table_name="openai_api_key")
    op.drop_index("ix_openai_api_key_status", table_name="openai_api_key")
    op.drop_table("openai_api_key")
