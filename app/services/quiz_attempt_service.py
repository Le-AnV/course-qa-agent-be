"""Student quiz delivery and attempt grading (AI-153 + AI-154).

AI-153 owns the attempt lifecycle: availability, start/resume, autosave and
submit. AI-154 owns grading: every submitted/expired attempt is scored once and
the result contract is snapshotted on the attempt. The two used to live in
separate modules, so this file is the single source of truth after the merge.

The service is deliberately the only place that decides whether an attempt is
still open. Clients render a countdown from ``server_time``/``deadline_at`` but
the server re-checks the deadline on every read and write, so a paused tab or a
clock-skewed machine cannot extend an attempt.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.course import Class, Enrollment
from app.models.quiz import Quiz, QuizAnswer, QuizAttempt, QuizQuestion
from app.models.user import User
from app.schemas.quiz import (
    QuizAnswerRead,
    QuizAttemptSessionRead,
    QuizQuestionStudentExamRead,
    QuizQuestionStudentResultRead,
    QuizTakeResultRead,
    StudentAnswerDraft,
    StudentQuizListItem,
    StudentQuizListRead,
)
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY
from app.services.quiz_grading import grade_question_submission

PUBLISHED_STATUS = "PUBLISHED"
IN_PROGRESS = "IN_PROGRESS"
SUBMITTED = "SUBMITTED"
EXPIRED = "EXPIRED"

GRADABLE_ATTEMPT_STATUSES = (SUBMITTED, EXPIRED)
SUPPORTED_QUESTION_TYPES = ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK")
DEFAULT_PASSING_PERCENTAGE = 50.0

#: A submit that lands a moment after the deadline still keeps the client's
#: last autosave; only the attempt status becomes EXPIRED.
SUBMIT_GRACE = timedelta(seconds=5)


class QuizAttemptError(ValueError):
    """Base error mapped to an HTTP response by the router."""

    status_code = 409


class QuizNotFoundError(QuizAttemptError):
    status_code = 404


class QuizForbiddenError(QuizAttemptError):
    status_code = 403


class QuizAnswerValidationError(QuizAttemptError):
    status_code = 422


class AttemptGradingError(QuizAttemptError):
    """Base class for errors raised while grading an attempt."""


class AttemptNotFoundError(AttemptGradingError):
    """The requested attempt does not exist."""

    status_code = 404


class AttemptNotGradableError(AttemptGradingError):
    """The attempt is still in progress or otherwise not submitted."""


class AttemptAnswerValidationError(AttemptGradingError):
    """Persisted student answers violate the AI-154 answer contract."""

    status_code = 422

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


def attempt_effective_passing_percentage(quiz: Quiz) -> float:
    """Return the quiz passing percentage, defaulting to 50% for legacy rows."""
    if quiz.passing_percentage is None:
        return DEFAULT_PASSING_PERCENTAGE
    return round_half_up(float(quiz.passing_percentage), 2)


def passing_score_for(quiz: Quiz, max_score: float) -> float:
    """Convert the quiz percentage to the point value shown to users."""
    return round_half_up(max_score * attempt_effective_passing_percentage(quiz) / 100)


def attempt_is_passed(
    total_score: float | None,
    max_score: float | None,
    passing_percentage: float | None,
) -> bool | None:
    """Compare percentages by cross-multiplication to avoid rounded-score drift."""
    if total_score is None or max_score is None or max_score <= 0:
        return None
    percentage = (
        float(passing_percentage)
        if passing_percentage is not None
        else DEFAULT_PASSING_PERCENTAGE
    )
    return Decimal(str(total_score)) * 100 >= Decimal(str(percentage)) * Decimal(str(max_score))


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _deadline(quiz: Quiz, attempt: QuizAttempt) -> datetime | None:
    started = _as_utc(attempt.started_at)
    if started is None or not quiz.time_limit_minutes:
        return None
    return started + timedelta(minutes=quiz.time_limit_minutes)


def _availability(quiz: Quiz, now: datetime) -> str:
    opens = _as_utc(quiz.opens_at)
    closes = _as_utc(quiz.closes_at)
    if opens is not None and now < opens:
        return "UPCOMING"
    if closes is not None and now > closes:
        return "CLOSED"
    return "OPEN"


def _load_quiz(db: Session, quiz_id: uuid.UUID, *, lock: bool = False) -> Quiz | None:
    stmt = (
        select(Quiz)
        .options(selectinload(Quiz.questions).selectinload(QuizQuestion.options))
        .where(Quiz.id == quiz_id)
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def _load_attempt(db: Session, attempt_id: uuid.UUID, *, lock: bool = False) -> QuizAttempt | None:
    stmt = (
        select(QuizAttempt)
        .options(selectinload(QuizAttempt.answers))
        .where(QuizAttempt.id == attempt_id)
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def _is_enrolled(db: Session, user_id: uuid.UUID, class_id: uuid.UUID) -> bool:
    row = (
        db.query(Enrollment.id)
        .join(Class, Enrollment.class_id == Class.id)
        .filter(
            Enrollment.student_id == user_id,
            Enrollment.class_id == class_id,
            Enrollment.status == "ACTIVE",
            Class.status != "ARCHIVED",
        )
        .first()
    )
    return row is not None


def _require_access(db: Session, user: User, quiz: Quiz | None) -> Quiz:
    if quiz is None:
        raise QuizNotFoundError("Quiz not found.")
    if not _is_enrolled(db, user.id, quiz.class_id):
        raise QuizForbiddenError("You are not a member of this class.")
    return quiz


def _require_published(quiz: Quiz) -> None:
    if quiz.status != PUBLISHED_STATUS:
        raise QuizNotFoundError("Quiz not found.")


def _require_open(quiz: Quiz, now: datetime) -> None:
    opens = _as_utc(quiz.opens_at)
    closes = _as_utc(quiz.closes_at)
    if opens is not None and now < opens:
        raise QuizAttemptError(f"This quiz opens at {opens.isoformat()}.")
    if closes is not None and now > closes:
        raise QuizAttemptError(f"This quiz closed at {closes.isoformat()}.")


def _answers_by_question(attempt: QuizAttempt) -> dict[uuid.UUID, QuizAnswer]:
    return {answer.question_id: answer for answer in attempt.answers}


def _answered(answer: QuizAnswer | None) -> bool:
    if answer is None:
        return False
    return bool(answer.selected_option_labels) or bool((answer.text_answer or "").strip())


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
    passing_percentage = attempt_effective_passing_percentage(quiz)
    passing_score = round_half_up(max_score * passing_percentage / 100)

    attempt.max_score = max_score
    attempt.total_score = total_score
    attempt.passing_percentage = passing_percentage
    attempt.passing_score = passing_score
    attempt.is_passed = attempt_is_passed(total_score, max_score, passing_percentage)
    attempt.graded_at = _now()

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


def _finalize(db: Session, quiz: Quiz, attempt: QuizAttempt, *, auto: bool) -> QuizAttempt:
    """Close and grade an attempt once. Safe to call on an already closed row."""
    if attempt.status != IN_PROGRESS:
        return attempt

    attempt.status = EXPIRED if auto else SUBMITTED
    attempt.submitted_at = _deadline(quiz, attempt) if auto else _now()
    db.flush()

    # AI-154 is the only grader. Keep the commit here so a grading failure
    # leaves the attempt untouched and retryable.
    grade_attempt(db, attempt.id, commit=False)
    db.commit()
    db.refresh(attempt)
    return attempt


def _ensure_graded(db: Session, attempt: QuizAttempt) -> None:
    """Grade a finalized attempt that predates the submit hook."""
    if attempt.status in GRADABLE_ATTEMPT_STATUSES and attempt.graded_at is None:
        grade_attempt(db, attempt.id, commit=False)
        db.commit()
        db.refresh(attempt)


def _result(quiz: Quiz, attempt: QuizAttempt) -> QuizTakeResultRead:
    max_total = (
        float(attempt.max_score)
        if attempt.max_score is not None
        else round(sum(float(question.points or 0) for question in quiz.questions), 4)
    )
    total = float(attempt.total_score or 0)
    passing_percentage = (
        float(attempt.passing_percentage)
        if attempt.passing_percentage is not None
        else attempt_effective_passing_percentage(quiz)
    )
    passing_score = (
        float(attempt.passing_score)
        if attempt.passing_score is not None
        else round_half_up(max_total * passing_percentage / 100)
    )

    from app.services.quiz_generator_service import compute_skill_radar_report

    answers_by_qid = {ans.question_id: ans for ans in attempt.answers}
    skill_radar = compute_skill_radar_report(quiz, answers_by_qid)

    show_solutions = getattr(quiz, "show_solutions_after_submission", True)
    if not show_solutions:
        # Sanitize questions so correct answers & explanations are hidden from students
        processed_questions: list[QuizQuestionStudentResultRead] = []
        for question in sorted(quiz.questions, key=lambda q: q.order_index):
            q_dict = {
                "id": question.id,
                "question_type": question.question_type,
                "question": question.question,
                "points": question.points,
                "difficulty": question.difficulty,
                "order_index": question.order_index,
                "explanation": None,
                "rubric": None,
                "source_reference": None,
                "options": [
                    {
                        "id": opt.id,
                        "option_label": opt.option_label,
                        "option_text": opt.option_text,
                        "order_index": opt.order_index,
                        "is_correct": False,
                    }
                    for opt in sorted(question.options, key=lambda o: o.order_index)
                ],
            }
            processed_questions.append(QuizQuestionStudentResultRead.model_validate(q_dict))
    else:
        processed_questions = [
            QuizQuestionStudentResultRead.model_validate(question)
            for question in sorted(quiz.questions, key=lambda q: q.order_index)
        ]

    return QuizTakeResultRead(
        attempt_id=attempt.id,
        quiz_id=quiz.id,
        quiz_title=quiz.title,
        status=attempt.status,
        started_at=_as_utc(attempt.started_at),
        submitted_at=_as_utc(attempt.submitted_at),
        total_score=total,
        max_score=max_total,
        percentage=attempt_percentage(total, max_total) or 0.0,
        passing_score=passing_score,
        passing_percentage=passing_percentage,
        is_passed=attempt.is_passed,
        show_solutions_after_submission=show_solutions,
        questions=processed_questions,
        answers=[QuizAnswerRead.model_validate(answer) for answer in attempt.answers],
        skill_radar=skill_radar,
    )


def _session(quiz: Quiz, attempt: QuizAttempt, now: datetime) -> QuizAttemptSessionRead:
    deadline = _deadline(quiz, attempt)
    remaining = (
        max(0, int((deadline - now).total_seconds())) if deadline is not None else None
    )
    finalized = attempt.status != IN_PROGRESS
    questions = (
        []
        if finalized
        else [
            QuizQuestionStudentExamRead.model_validate(question)
            for question in sorted(quiz.questions, key=lambda q: q.order_index)
        ]
    )
    return QuizAttemptSessionRead(
        attempt_id=attempt.id,
        quiz_id=quiz.id,
        quiz_title=quiz.title,
        quiz_description=quiz.description,
        time_limit_minutes=quiz.time_limit_minutes,
        status=attempt.status,
        started_at=_as_utc(attempt.started_at),
        deadline_at=deadline,
        server_time=now,
        remaining_seconds=remaining,
        questions=questions,
        answers=[QuizAnswerRead.model_validate(answer) for answer in attempt.answers],
        total_points=round(sum(float(question.points or 0) for question in quiz.questions), 4),
        answered_count=sum(1 for answer in attempt.answers if _answered(answer)),
        result=_result(quiz, attempt) if finalized else None,
    )


def list_student_quizzes(
    db: Session, user: User, class_id: uuid.UUID | None = None
) -> StudentQuizListRead:
    """Published quizzes in the classes the caller is actively enrolled in."""
    now = _now()
    rows = (
        db.query(Quiz, Class)
        .join(Class, Quiz.class_id == Class.id)
        .join(Enrollment, Enrollment.class_id == Class.id)
        .options(selectinload(Quiz.questions).selectinload(QuizQuestion.options))
        .filter(
            Enrollment.student_id == user.id,
            Enrollment.status == "ACTIVE",
            Class.status != "ARCHIVED",
            Quiz.status == PUBLISHED_STATUS,
        )
        .order_by(Class.code, Quiz.created_at.desc())
    )
    if class_id is not None:
        rows = rows.filter(Quiz.class_id == class_id)
    rows = rows.all()
    if not rows:
        return StudentQuizListRead(server_time=now, quizzes=[])

    quiz_ids = [quiz.id for quiz, _ in rows]
    attempts = (
        db.execute(
            select(QuizAttempt)
            .options(selectinload(QuizAttempt.answers))
            .where(
                QuizAttempt.student_id == user.id,
                QuizAttempt.quiz_id.in_(quiz_ids),
            )
            .order_by(QuizAttempt.started_at.desc())
        )
        .scalars()
        .all()
    )
    by_quiz: dict[uuid.UUID, list[QuizAttempt]] = {}
    for attempt in attempts:
        by_quiz.setdefault(attempt.quiz_id, []).append(attempt)

    changed = False
    items: list[StudentQuizListItem] = []
    for quiz, clazz in rows:
        quiz_attempts = by_quiz.get(quiz.id, [])
        active = next(
            (attempt for attempt in quiz_attempts if attempt.status == IN_PROGRESS), None
        )
        if active is not None:
            deadline = _deadline(quiz, active)
            if deadline is not None and now >= deadline:
                _finalize(db, quiz, active, auto=True)
                changed = True

        for attempt in quiz_attempts:
            if attempt.status in GRADABLE_ATTEMPT_STATUSES and attempt.graded_at is None:
                grade_attempt(db, attempt.id, commit=False)
                changed = True

        used = len(quiz_attempts)
        left = None if quiz.max_attempts is None else max(0, quiz.max_attempts - used)
        availability = _availability(quiz, now)
        active = next(
            (attempt for attempt in quiz_attempts if attempt.status == IN_PROGRESS), None
        )
        can_start = active is not None or (
            availability == "OPEN" and (left is None or left > 0)
        )
        if active is None and left == 0:
            availability = "EXHAUSTED"
        official = select_official_attempts(quiz_attempts).get(user.id)
        latest = official or next(
            (attempt for attempt in quiz_attempts if attempt.status != IN_PROGRESS), None
        )
        total_points = round_half_up(
            sum(float(question.points or 0) for question in quiz.questions)
        )
        passage = attempt_effective_passing_percentage(quiz)
        items.append(
            StudentQuizListItem(
                quiz_id=quiz.id,
                class_id=clazz.id,
                class_code=clazz.code,
                class_name=clazz.name,
                title=quiz.title,
                description=quiz.description,
                time_limit_minutes=quiz.time_limit_minutes,
                opens_at=_as_utc(quiz.opens_at),
                closes_at=_as_utc(quiz.closes_at),
                max_attempts=quiz.max_attempts,
                attempts_used=used,
                attempts_left=left,
                availability=availability,
                attempt_id=active.id if active else (latest.id if latest else None),
                attempt_status=(
                    active.status if active else (latest.status if latest else None)
                ),
                can_start=can_start,
                total_score=latest.total_score if latest else None,
                max_score=(
                    latest.max_score if latest is not None and latest.max_score is not None else total_points
                ),
                percentage=(
                    attempt_percentage(latest.total_score, latest.max_score)
                    if latest is not None
                    else None
                ),
                passing_score=passing_score_for(quiz, total_points),
                passing_percentage=passage,
                is_passed=latest.is_passed if latest else None,
                official_attempt_id=official.id if official is not None else None,
            )
        )

    if changed:
        db.commit()
    return StudentQuizListRead(server_time=now, quizzes=items)


def start_or_resume(db: Session, user: User, quiz_id: uuid.UUID) -> QuizAttemptSessionRead:
    """Start a new attempt, resume the active one, or return a finalized one."""
    now = _now()
    quiz = _load_quiz(db, quiz_id, lock=True)
    _require_published(_require_access(db, user, quiz))
    assert quiz is not None  # narrowed by _require_access

    attempts = (
        db.execute(
            select(QuizAttempt)
            .options(selectinload(QuizAttempt.answers))
            .where(QuizAttempt.quiz_id == quiz.id, QuizAttempt.student_id == user.id)
            .order_by(QuizAttempt.started_at.desc())
        )
        .scalars()
        .all()
    )
    active = next((attempt for attempt in attempts if attempt.status == IN_PROGRESS), None)
    if active is not None:
        deadline = _deadline(quiz, active)
        if deadline is not None and now >= deadline:
            _finalize(db, quiz, active, auto=True)
            return _session(quiz, active, _now())
        return _session(quiz, active, now)

    _require_open(quiz, now)
    if quiz.max_attempts is not None and len(attempts) >= quiz.max_attempts:
        raise QuizAttemptError("No attempts left for this quiz.")

    attempt = QuizAttempt(quiz_id=quiz.id, student_id=user.id, status=IN_PROGRESS)
    db.add(attempt)
    try:
        db.commit()
    except IntegrityError:
        # A second tab/request won the race; resume that attempt instead.
        db.rollback()
        active = (
            db.execute(
                select(QuizAttempt)
                .options(selectinload(QuizAttempt.answers))
                .where(
                    QuizAttempt.quiz_id == quiz.id,
                    QuizAttempt.student_id == user.id,
                    QuizAttempt.status == IN_PROGRESS,
                )
            )
            .scalars()
            .first()
        )
        if active is None:
            raise
        return _session(quiz, active, _now())
    db.refresh(attempt)
    return _session(quiz, attempt, _now())


def _apply_drafts(
    db: Session, quiz: Quiz, attempt: QuizAttempt, drafts: list[StudentAnswerDraft]
) -> None:
    questions = {question.id: question for question in quiz.questions}
    existing = _answers_by_question(attempt)
    # Last write wins when a client retries the same question in one batch.
    pending: dict[uuid.UUID, StudentAnswerDraft] = {draft.question_id: draft for draft in drafts}

    for question_id, draft in pending.items():
        question = questions.get(question_id)
        if question is None:
            raise QuizAnswerValidationError("Answer does not belong to this quiz.")

        labels = draft.selected_option_labels or []
        if question.question_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            allowed = {option.option_label for option in question.options}
            if any(label not in allowed for label in labels):
                raise QuizAnswerValidationError("Answer selects an option that does not exist.")
            if question.question_type == "SINGLE_CHOICE" and len(labels) > 1:
                raise QuizAnswerValidationError(
                    "A single-choice question accepts only one option."
                )
            if draft.text_answer:
                raise QuizAnswerValidationError(
                    "A choice question does not accept a text answer."
                )
        else:
            if labels:
                raise QuizAnswerValidationError(
                    "A fill-in-the-blank question does not accept options."
                )

        answer = existing.get(question_id)
        empty = not labels and not (draft.text_answer or "").strip()
        if empty and not draft.is_marked_for_review:
            if answer is not None:
                db.delete(answer)
                # Keep the in-memory collection in sync so submit grades only
                # the answers that survive the flush.
                attempt.answers.remove(answer)
                existing.pop(question_id, None)
            continue
        if answer is None:
            answer = QuizAnswer(question_id=question_id)
            attempt.answers.append(answer)
            existing[question_id] = answer

        answer.selected_option_labels = labels or None
        answer.text_answer = draft.text_answer
        answer.is_marked_for_review = draft.is_marked_for_review


def save_draft(
    db: Session,
    user: User,
    attempt_id: uuid.UUID,
    drafts: list[StudentAnswerDraft],
) -> QuizAttemptSessionRead:
    """Upsert the autosaved answers of an active attempt."""
    attempt = _load_attempt(db, attempt_id, lock=True)
    if attempt is None or attempt.student_id != user.id:
        raise QuizNotFoundError("Attempt not found.")
    quiz = _require_access(db, user, _load_quiz(db, attempt.quiz_id))
    if attempt.status != IN_PROGRESS:
        raise QuizAttemptError("This attempt was already submitted.")

    now = _now()
    deadline = _deadline(quiz, attempt)
    if deadline is not None and now >= deadline:
        _finalize(db, quiz, attempt, auto=True)
        raise QuizAttemptError("Time is up. This attempt was submitted automatically.")

    _apply_drafts(db, quiz, attempt, drafts)
    db.commit()
    db.refresh(attempt)
    return _session(quiz, attempt, _now())


def submit_attempt(
    db: Session,
    user: User,
    attempt_id: uuid.UUID,
    drafts: list[StudentAnswerDraft] | None = None,
) -> QuizAttemptSessionRead:
    """Grade once and return the result; repeated calls return the same attempt.

    AC-06: the row is locked, so two concurrent submit clicks cannot create a
    second attempt or a second score. A repeated call on a closed attempt is a
    no-op that returns the stored result.
    """
    attempt = _load_attempt(db, attempt_id, lock=True)
    if attempt is None or attempt.student_id != user.id:
        raise QuizNotFoundError("Attempt not found.")
    quiz = _require_access(db, user, _load_quiz(db, attempt.quiz_id))

    if attempt.status != IN_PROGRESS:
        _ensure_graded(db, attempt)
        return _session(quiz, attempt, _now())

    now = _now()
    deadline = _deadline(quiz, attempt)
    if drafts:
        _apply_drafts(db, quiz, attempt, drafts)
        db.flush()

    auto = deadline is not None and now > deadline + SUBMIT_GRACE
    _finalize(db, quiz, attempt, auto=auto)
    return _session(quiz, attempt, _now())


def get_attempt(db: Session, user: User, attempt_id: uuid.UUID) -> QuizAttemptSessionRead:
    """Reload an attempt after a refresh or a dropped connection."""
    attempt = _load_attempt(db, attempt_id)
    if attempt is None or attempt.student_id != user.id:
        raise QuizNotFoundError("Attempt not found.")
    quiz = _require_access(db, user, _load_quiz(db, attempt.quiz_id))

    now = _now()
    if attempt.status == IN_PROGRESS:
        deadline = _deadline(quiz, attempt)
        if deadline is not None and now >= deadline:
            _finalize(db, quiz, attempt, auto=True)
    else:
        _ensure_graded(db, attempt)
    return _session(quiz, attempt, _now())


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
