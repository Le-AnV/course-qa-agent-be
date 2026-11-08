"""Merge the AI-160 question bank and quiz grading migration branches.

Revision ID: 0044_merge_qbank_quiz_heads
Revises: 0042_question_bank, 0043_merge_skill_quiz_heads
Create Date: 2026-11-08

AI-160 (``0042_question_bank``) and the quiz grading integration
(``0043_merge_skill_quiz_heads``) both land on ``0041_skill_taxonomy``.
This empty revision joins them so ``alembic heads`` stays at one.
"""

revision = "0044_merge_qbank_quiz_heads"
down_revision = ("0042_question_bank", "0043_merge_skill_quiz_heads")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
