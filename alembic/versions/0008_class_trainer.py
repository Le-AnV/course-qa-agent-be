"""Add class_trainer: extra trainers per class (co-teaching / handover).

``class.trainer_id`` stays the primary trainer; this table holds the extra
trainers who can also work on the class. Existing classes need no backfill —
access checks read the union of the primary slot and these rows.

Revision ID: 0008_class_trainer
Revises: 0007_student_code
Create Date: 2026-10-18
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_class_trainer"
down_revision = "0007_student_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "class_trainer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("trainer_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["class_id"], ["class.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trainer_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("class_id", "trainer_id", name="uq_class_trainer"),
    )
    op.create_index("ix_class_trainer_class_id", "class_trainer", ["class_id"])
    op.create_index("ix_class_trainer_trainer_id", "class_trainer", ["trainer_id"])


def downgrade() -> None:
    op.drop_index("ix_class_trainer_trainer_id", table_name="class_trainer")
    op.drop_index("ix_class_trainer_class_id", table_name="class_trainer")
    op.drop_table("class_trainer")
