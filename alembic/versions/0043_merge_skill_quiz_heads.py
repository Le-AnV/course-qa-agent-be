"""Merge the AI-159 skill taxonomy and AI-154 quiz results migration branches.

Revision ID: 0043_merge_skill_quiz_heads
Revises: 0041_skill_taxonomy, 0042_quiz_passing_percentage
Create Date: 2026-11-08

AI-159 (``0041_skill_taxonomy``) and the quiz grading integration
(``0042_quiz_passing_percentage``) both branched from ``0040_quiz_availability``.
This empty revision joins them so ``alembic heads`` stays at one.
"""

revision = "0043_merge_skill_quiz_heads"
down_revision = ("0041_skill_taxonomy", "0042_quiz_passing_percentage")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
