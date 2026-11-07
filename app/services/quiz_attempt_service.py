"""Attempt-level objective grading and result aggregation (AI-154).

AI-153 owns the attempt lifecycle (start / autosave / submit). This module only
grades an attempt that is already ``SUBMITTED`` or ``EXPIRED``, snapshots the
score contract on the attempt, and exposes deterministic helpers used by the
student result and teacher statistics endpoints.

Scoring follows the AI-148 contract in :mod:`app.services.quiz_grading`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session, selectinload

from app.models.quiz import Quiz, QuizAnswer, QuizAttempt, QuizQuestion
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY
from app.services.quiz_grading import grade_question_submission

GRADABLE_ATTEMPT_STATUSES = ("SUBMITTED", "EXPIRED")
SUPPORTED_QUESTION_TYPES = ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK")


class AttemptGradingError(ValueError):
    """Base class for errors raised while grading an attempt."""


class AttemptNotFoundError(AttemptGradingError):
    """The requested attempt does not exist."""


class AttemptNotGradableError(AttemptGradingError):
    """The attempt is still in progress or otherwise not submitted."""


class AttemptAnswerValidationError(AttemptGradingError):
    """Persisted student answers violate the AI-154 answer contract."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def round_half_up(value: float, places: int = 2) -> float:
    """Round ``value`` with ``ROUND_HALF_UP`` instead of Python's banker's rounding."""
    quantizer = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP))


def attempt_percentage(total_score: float | None, max_score: float | None) -> float | None:
    """Return the attempt percentage, or ``None`` when it cannot be calculated."""
    if total_score is None or max_score is None or max_score <= 0:
        return None
    return round_half_up((total_score / max_score) * 100)


def attempt_effective_passing_score(quiz: Quiz, max_score: float) -> float:
    """Snapshot the quiz passing score, defaulting to 50% of ``max_score``."""
    if quiz.passing_score is not None:
        return round_half_up(quiz.passing_score)
    return round_half_up(max_score * 0.5)


def _normalised_labels(labels: list[str] | None) -> list[str]:
    return [str(label).strip().upper() for label in (labels or [])]


def _validate_answer(question: QuizQuestion, answer: QuizAnswer | None) -> list[str]:
    """Validate one persisted answer before any score is written.

    Running validation for the whole attempt first guarantees a failed grading
    leaves ``total_score`` null and the attempt retryable (AC-05).
    """
    errors: list[str] = []
    question_type = (question.question_type or "").upper()
    if question_type not in SUPPORTED_QUESTION_TYPES:
        errors.append(
            f"Question {question.id}: unsupported question type {question.question_type!r}."
        )
        return errors

    selected = _normalised_labels(answer.selected_option_labels if answer else None)
    if len(selected) != len(set(selected)):
        errors.append(f"Question {question.id}: duplicate selected option labels.")

    if question_type == "SINGLE_CHOICE" and len(selected) > 1:
        errors.append(
            f"Question {question.id}: SINGLE_CHOICE accepts at most one selected option."
        )

    option_labels = {option.option_label.strip().upper() for option in question.options}
    unknown = sorted(set(selected) - option_labels)
    if unknown:
        errors.append(
            f"Question {question.id}: selected labels {unknown} do not belong to the question."
        )
    return errors


def _grade_question(
    question: QuizQuestion, answer: QuizAnswer
) -> tuple[float, bool, dict]:
    """Grade one question and return ``(score, is_correct, breakdown)``."""
    grading_config = question.grading_config or {}
    result = grade_question_submission(
        question_type=question.question_type,
        points=question.points,
        options_data=[
            {
                "option_label": option.option_label,
                "is_correct": option.is_correct,
            }
            for option in question.options
        ],
        student_selected_labels=answer.selected_option_labels,
        student_text=answer.text_answer,
        accepted_text_answers=grading_config.get(FILL_IN_BLANK_ANSWERS_KEY),
        grading_config=grading_config,
    )
    breakdown = dict(result.breakdown or {})
    breakdown["max_points"] = round_half_up(result.max_points)
    return round_half_up(result.score), result.is_correct, breakdown


def grade_attempt(db: Session, attempt_id: uuid.UUID, *, commit: bool = True) -> QuizAttempt:
    """Grade a submitted attempt idempotently under a row lock.

    AI-153 calls this after a successful submit (and after auto-submit). A
    repeated call returns the existing result without recomputing scores.
    """
    attempt = (
        db.query(QuizAttempt)
        .filter(QuizAttempt.id == attempt_id)
        .with_for_update()
        .one_or_none()
    )
    if attempt is None:
        raise AttemptNotFoundError(f"Quiz attempt {attempt_id} was not found.")
    if attempt.graded_at is not None:
        return attempt
    if attempt.status not in GRADABLE_ATTEMPT_STATUSES:
        raise AttemptNotGradableError(
            f"Attempt {attempt.id} is {attempt.status}; only SUBMITTED or EXPIRED attempts "
            "can be graded."
        )

    quiz = (
        db.query(Quiz)
        .options(selectinload(Quiz.questions).selectinload(QuizQuestion.options))
        .filter(Quiz.id == attempt.quiz_id)
        .one_or_none()
    )
    if quiz is None:
        raise AttemptGradingError(f"Quiz {attempt.quiz_id} was not found for attempt {attempt.id}.")

    existing_answers = {
        answer.question_id: answer
        for answer in db.query(QuizAnswer).filter(QuizAnswer.attempt_id == attempt.id).all()
    }

    # Validate the entire attempt before mutating anything (AC-05 retryability).
    validation_errors: list[str] = []
    for question in quiz.questions:
        validation_errors.extend(_validate_answer(question, existing_answers.get(question.id)))
    if validation_errors:
        raise AttemptAnswerValidationError(validation_errors)

    total_score = 0.0
    max_score = 0.0
    for question in quiz.questions:
        answer = existing_answers.get(question.id)
        if answer is None:
            # Unanswered questions are still materialised so result and statistics
            # queries can treat every question uniformly.
            answer = QuizAnswer(attempt_id=attempt.id, question_id=question.id)
            db.add(answer)
        score, is_correct, breakdown = _grade_question(question, answer)
        answer.score_awarded = score
        answer.is_correct = is_correct
        answer.breakdown = breakdown
        total_score += score
        max_score += question.points

    max_score = round_half_up(max_score)
    total_score = round_half_up(total_score)
    passing_score = attempt_effective_passing_score(quiz, max_score)

    attempt.max_score = max_score
    attempt.passing_score = passing_score
    attempt.total_score = total_score
    attempt.is_passed = total_score >= passing_score
    attempt.graded_at = datetime.now(UTC)

    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(attempt)
    else:
        db.flush()
    return attempt


def _as_aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _official_key(attempt: QuizAttempt) -> tuple[float, datetime, datetime, str]:
    """Highest score wins; ties go to the latest submission, then latest grading."""
    return (
        attempt.total_score if attempt.total_score is not None else float("-inf"),
        _as_aware_utc(attempt.submitted_at),
        _as_aware_utc(attempt.graded_at),
        str(attempt.id),
    )


def select_official_attempts(
    attempts: Iterable[QuizAttempt],
) -> dict[uuid.UUID, QuizAttempt]:
    """Return the official attempt per student.

    Official = highest graded score; ties are broken by the latest
    ``submitted_at`` (then the latest ``graded_at`` for deterministic output).
    """
    official: dict[uuid.UUID, QuizAttempt] = {}
    for attempt in attempts:
        if attempt.status not in GRADABLE_ATTEMPT_STATUSES:
            continue
        if attempt.graded_at is None or attempt.total_score is None:
            continue
        current = official.get(attempt.student_id)
        if current is None or _official_key(attempt) > _official_key(current):
            official[attempt.student_id] = attempt
    return official


def official_attempt_ids_for_quiz(
    db: Session,
    quiz_id: uuid.UUID,
    *,
    student_ids: Iterable[uuid.UUID] | None = None,
) -> dict[uuid.UUID, uuid.UUID]:
    """Map each student to their official attempt id for one quiz."""
    query = db.query(QuizAttempt).filter(
        QuizAttempt.quiz_id == quiz_id,
        QuizAttempt.graded_at.is_not(None),
        QuizAttempt.total_score.is_not(None),
    )
    if student_ids is not None:
        ids = list(student_ids)
        if not ids:
            return {}
        query = query.filter(QuizAttempt.student_id.in_(ids))
    return {
        student_id: attempt.id
        for student_id, attempt in select_official_attempts(query.all()).items()
    }
