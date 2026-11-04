"""Add the ticket foreign key to ticket_handover.

``ticket_handover`` was created in 0028 before the ``ticket`` table existed, so
``ticket_id`` was left as a bare UUID. AI-81/AI-83 have since shipped the table,
so the deferred foreign key can land now (AI-155 AC-04).
"""

import sqlalchemy as sa

from alembic import op

revision = "0036_ticket_handover_fk"
down_revision = "0035_answer_feedback_ticket"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ticket_handover") as batch:
        batch.create_foreign_key(
            "fk_ticket_handover_ticket",
            "ticket",
            ["ticket_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("ticket_handover") as batch:
        batch.drop_constraint("fk_ticket_handover_ticket", type_="foreignkey")
