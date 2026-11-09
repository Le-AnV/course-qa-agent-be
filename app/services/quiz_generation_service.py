"""Per-question regeneration seam with the AI-150 validation and retry policy.

The generator itself belongs to AI-149. This module keeps the AI-151 HTTP
contract stable: it calls the installed regenerator, validates every candidate
with the shared AI-150 validator, retries only when the candidate is invalid or
duplicates another question, and leaves the stored question untouched on failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.quiz import QuizQuestion
from app.models.user import User
from app.schemas.quiz import QuizOptionCreate, QuizQuestionCreate
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY
from app.services.quiz_validation import (
    MAX_REGENERATION_ATTEMPTS,
    GeneratedQuestion,
    ValidationIssue,
    ValidationSpec,
    validate_batch,
)

Regenerator = Callable[..., QuizQuestionCreate | GeneratedQuestion]

_regenerator: Regenerator | None = None


@dataclass(frozen=True)
class RegeneratedQuestion:
    """Trusted regeneration result: the question plus server-owned metadata."""

    question: QuizQuestionCreate
    confidence: float | None = None
    validation_flags: tuple[str, ...] = ()


class QuizGenerationUnavailable(RuntimeError):
    """Raised when no question generator is configured."""


class QuizGenerationValidationError(ValueError):
    """The generator exhausted its attempts without a valid replacement."""

    def __init__(self, message: str, issues: list[ValidationIssue] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


def set_regenerator(regenerator: Regenerator | None) -> None:
    """Install the AI-149 generator implementation (tests, or the real service)."""

    global _regenerator
    _regenerator = regenerator


def _payload_from_question(question: QuizQuestion) -> QuizQuestionCreate:
    grading_config = dict(question.grading_config or {})
    return QuizQuestionCreate(
        question_type=question.question_type,
        question=question.question,
        points=question.points,
        difficulty=question.difficulty,
        order_index=question.order_index,
        explanation=question.explanation,
        rubric=question.rubric,
        source_reference=question.source_reference,
        grading_config=question.grading_config,
        options=[
            QuizOptionCreate(
                option_label=option.option_label,
                option_text=option.option_text,
                is_correct=option.is_correct,
                order_index=option.order_index,
            )
            for option in question.options
        ],
        correct_answers=list(grading_config.get(FILL_IN_BLANK_ANSWERS_KEY, [])),
    )


def _candidate(generated: QuizQuestionCreate | GeneratedQuestion) -> GeneratedQuestion:
    if isinstance(generated, GeneratedQuestion):
        return generated
    return GeneratedQuestion(data=generated)


def regenerate_question(
    *,
    db: Session,
    question: QuizQuestion,
    instructions: str | None,
    requested_by: User,
) -> RegeneratedQuestion:
    """Generate one replacement question for ``question``.

    The replacement must keep the original question type, points and difficulty,
    must not duplicate a sibling question, and must pass the AI-150 validator.
    Invalid or duplicate candidates are retried with the previous failure reasons;
    after the bounded attempts the stored question is left untouched.
    """

    if _regenerator is None:
        raise QuizGenerationUnavailable(
            "Question generation is not available right now."
        )

    siblings = [
        _payload_from_question(sibling)
        for sibling in question.quiz.questions
        if sibling.id != question.id
    ]
    spec = ValidationSpec(
        total_questions=1,
        type_counts={question.question_type: 1},
    )

    feedback = ""
    last_issues: list[ValidationIssue] = []

    for _ in range(MAX_REGENERATION_ATTEMPTS):
        attempt_instructions = instructions
        if feedback:
            retry_note = f"Previous attempt was rejected: {feedback}"
            attempt_instructions = (
                f"{instructions}\n\n{retry_note}" if instructions else retry_note
            )

        try:
            generated = _regenerator(
                db=db,
                question=question,
                instructions=attempt_instructions,
                requested_by=requested_by,
            )
            candidate = _candidate(generated)
        except QuizGenerationUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - generator boundary
            last_issues = [
                ValidationIssue(
                    code="GENERATOR_ERROR",
                    message=f"{type(exc).__name__}: {exc}",
                )
            ]
            feedback = last_issues[0].message
            continue

        result = validate_batch(
            [candidate],
            spec,
            existing=siblings,
        )
        if result.questions:
            accepted = result.questions[0].model_copy(
                update={
                    "points": question.points,
                    "difficulty": question.difficulty,
                }
            )
            return RegeneratedQuestion(
                question=accepted,
                confidence=result.results[0].confidence,
                validation_flags=tuple(result.results[0].flags),
            )

        last_issues = result.results[0].issues
        feedback = (
            "; ".join(f"{issue.code}: {issue.message}" for issue in last_issues)
            or "Invalid question."
        )

    raise QuizGenerationValidationError(
        "The generator could not produce a valid question. "
        "Your current question was not changed.",
        last_issues,
    )
