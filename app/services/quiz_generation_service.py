"""Seam for per-question regeneration (AI-151 AC-01).

The generator itself belongs to AI-149 (generate from documents) and the
retry/validation policy to AI-150. They are still in progress, so this module
defines the contract AI-151 needs and nothing else: when those stories land,
only :func:`regenerate_question` changes, not the HTTP route.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models.quiz import QuizQuestion
from app.models.user import User
from app.schemas.quiz import QuizQuestionCreate

Regenerator = Callable[..., QuizQuestionCreate]

_regenerator: Regenerator | None = None


class QuizGenerationUnavailable(RuntimeError):
    """Raised when no question generator is configured."""


def set_regenerator(regenerator: Regenerator | None) -> None:
    """Install the AI-149/AI-150 implementation (tests, or the real service)."""
    global _regenerator
    _regenerator = regenerator


def regenerate_question(
    *,
    db: Session,
    question: QuizQuestion,
    instructions: str | None,
    requested_by: User,
) -> QuizQuestionCreate:
    """Generate one replacement question for ``question``.

    Preserves the question type, points, and difficulty. Raises
    :class:`QuizGenerationUnavailable` when no generator is installed, and lets
    the caller reject output that fails AI-148 validation.
    """
    if _regenerator is None:
        raise QuizGenerationUnavailable(
            "Question generation is not available right now."
        )
    return _regenerator(
        db=db,
        question=question,
        instructions=instructions,
        requested_by=requested_by,
    )
