"""Persist AI-150 validation flags on generated quiz questions."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0040_quiz_validation"
down_revision = "0045_quiz_show_solutions"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("quiz_question") as batch:
        batch.add_column(sa.Column("confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("validation_flags", JSON_TYPE, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("quiz_question") as batch:
        batch.drop_column("validation_flags")
        batch.drop_column("confidence")
