"""Merge the content gap and ticket handover heads.

AI-91 (``0035_class_category``) and AI-155 (``0035_answer_feedback_ticket`` ->
``0036_ticket_handover_fk``) both branched from ``0034_ticket_escalation`` and
landed on ``main`` separately, leaving two alembic heads. This merge revision
brings them back to a single head so ``alembic upgrade head`` works again.
"""

revision = "0037_merge_content_gap_and_handover"
down_revision = ("0035_class_category", "0036_ticket_handover_fk")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
