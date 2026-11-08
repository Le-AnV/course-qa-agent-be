"""Merge the AI-153 availability and AI-154 grading migration branches.

Revision ID: 0041_merge_quiz_heads
Revises: 0039_quiz_grading_results, 0040_quiz_availability
Create Date: 2026-11-08

AI-153 and AI-154 were developed in parallel from ``0039_quiz_approval`` and
both landed as heads. This empty revision joins them so the following schema
migration can depend on both branches and ``alembic heads`` stays at one.
"""

revision = "0041_merge_quiz_heads"
down_revision = ("0039_quiz_grading_results", "0040_quiz_availability")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
