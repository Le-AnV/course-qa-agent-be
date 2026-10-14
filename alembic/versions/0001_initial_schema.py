"""Initial schema: all metadata tables (idempotent on existing DBs).

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-13

Adopting Alembic mid-project: existing databases were created by
``Base.metadata.create_all`` (same table set), so ``create_all`` here is a
no-op for them — they only need ``alembic stamp head``. Fresh clones run
``alembic upgrade head`` to build the schema from scratch.
"""

from alembic import op
from app import models  # noqa: F401  (register tables on Base.metadata)
from app.core.database import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
