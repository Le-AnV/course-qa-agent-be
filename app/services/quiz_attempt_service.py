"""Student quiz delivery: availability, attempts, autosave drafts and submit (AI-153).

The service is deliberately the only place that decides whether an attempt is
still open. Clients render a countdown from ``server_time``/``deadline_at`` but
the server re-checks the deadline on every read and write, so a paused tab or a
clock-skewed machine cannot extend an attempt.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.course import Class, Enrollment
from app.models.quiz import Quiz, QuizAnswer, QuizAttempt, QuizQuestion
from app.models.user import User
from app.schemas.quiz import (
    QuizAnswerRead,
    QuizAttemptResultRead,
    QuizAttemptSessionRead,
    QuizQuestionStudentExamRead,
    QuizQuestionStudentResultRead,
    StudentAnswerDraft,
    StudentQuizListItem,
    StudentQuizListRead,
)
from app.services.quiz_grading import grade_question_submission

PUBLISHED_STATUS = "PUBLISHED"
IN_PROGRESS = "IN_PROGRESS"
SUBMITTED = "SUBMITTED"
EXPIRED = "EXPIRED"

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


def _score_attempt(quiz: Quiz, attempt: QuizAttempt) -> tuple[float, float]:
    """Grade every saved answer and return ``(total, max_total)``."""
    by_question = _answers_by_question(attempt)
    total = 0.0
    max_total = 0.0
    for question in quiz.questions:
        max_total += float(question.points or 0)
        answer = by_question.get(question.id)
        graded = grade_question_submission(
            question_type=question.question_type,
            points=float(question.points or 0),
            options_data=[
                {"option_label": option.option_label, "is_correct": option.is_correct}
                for option in question.options
            ],
            student_selected_labels=answer.selected_option_labels if answer else None,
            student_text=answer.text_answer if answer else None,
            accepted_text_answers=(question.grading_config or {}).get("correct_answers") or [],
            grading_config=question.grading_config,
        )
        if answer is not None:
            answer.score_awarded = graded.score
            answer.breakdown = graded.breakdown
        total += graded.score
    return round(total, 4), round(max_total, 4)


def _percentage(total: float, max_total: float) -> float:
    if max_total <= 0:
        return 0.0
    # Negative multiple-choice penalties never produce a negative percentage.
    return round(max(0.0, total / max_total * 100), 2)


def _finalize(db: Session, quiz: Quiz, attempt: QuizAttempt, *, auto: bool) -> QuizAttempt:
    """Grade and close an attempt once. Safe to call on an already closed row."""
    if attempt.status != IN_PROGRESS:
        return attempt

    total, max_total = _score_attempt(quiz, attempt)
    percentage = _percentage(total, max_total)
    attempt.status = EXPIRED if auto else SUBMITTED
    attempt.submitted_at = _deadline(quiz, attempt) if auto else _now()
    attempt.total_score = total
    if quiz.passing_score is None:
        attempt.is_passed = None
    else:
        attempt.is_passed = percentage >= float(quiz.passing_score)
    db.commit()
    db.refresh(attempt)
    return attempt


def _result(quiz: Quiz, attempt: QuizAttempt) -> QuizAttemptResultRead:
    total = float(attempt.total_score or 0)
    max_total = round(sum(float(question.points or 0) for question in quiz.questions), 4)
    percentage = _percentage(total, max_total)
    return QuizAttemptResultRead(
        attempt_id=attempt.id,
        quiz_id=quiz.id,
        quiz_title=quiz.title,
        status=attempt.status,
        started_at=_as_utc(attempt.started_at),
        submitted_at=_as_utc(attempt.submitted_at),
        total_score=total,
        max_score=max_total,
        percentage=percentage,
        passing_score=quiz.passing_score,
        is_passed=attempt.is_passed,
        questions=[
            QuizQuestionStudentResultRead.model_validate(question)
            for question in sorted(quiz.questions, key=lambda q: q.order_index)
        ],
        answers=[QuizAnswerRead.model_validate(answer) for answer in attempt.answers],
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
        latest = next(
            (attempt for attempt in quiz_attempts if attempt.status != IN_PROGRESS), None
        )
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
                attempt_status=active.status if active else (latest.status if latest else None),
                can_start=can_start,
                total_score=latest.total_score if latest else None,
                is_passed=latest.is_passed if latest else None,
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
    return _session(quiz, attempt, _now())
