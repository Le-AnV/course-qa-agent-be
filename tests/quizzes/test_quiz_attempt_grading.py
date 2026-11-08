"""Tests for attempt-level objective grading and official-attempt selection (AI-154)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.core.database import Base
from app.models.course import Class
from app.models.quiz import (
    Quiz,
    QuizAnswer,
    QuizAttempt,
    QuizOption,
    QuizQuestion,
)
from app.models.user import Role, User
from app.services.quiz_attempt_service import (
    AttemptAnswerValidationError,
    AttemptNotGradableError,
    attempt_is_passed,
    attempt_percentage,
    grade_attempt,
    round_half_up,
    select_official_attempts,
)
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY

SUBMITTED_AT = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _create_user(db: Session, role_name: str, email: str, full_name: str) -> User:
    role = db.query(Role).filter_by(name=role_name).first()
    if role is None:
        role = Role(name=role_name)
        db.add(role)
        db.flush()
    user = User(
        id=uuid.uuid4(),
        full_name=full_name,
        email=email,
        role_id=role.id,
        status="ACTIVE",
    )
    db.add(user)
    db.flush()
    return user


def _seed_quiz(
    db: Session,
    *,
    passing_percentage: float | None = None,
    show_explanations: bool = False,
    status: str = "SUBMITTED",
    submitted_at: datetime | None = SUBMITTED_AT,
) -> dict:
    """Create one quiz with single/multi/fill questions and a submitted attempt."""
    trainer = _create_user(db, "TRAINER", "trainer@example.com", "Trainer")
    student = _create_user(db, "STUDENT", "student@example.com", "Student")
    clazz = Class(
        id=uuid.uuid4(),
        trainer_id=trainer.id,
        code="AI154-A",
        name="AI-154 Class",
        credits=3,
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db.add(clazz)
    db.flush()

    quiz = Quiz(
        id=uuid.uuid4(),
        class_id=clazz.id,
        creator_id=trainer.id,
        title="Objective Quiz",
        passing_percentage=50.0 if passing_percentage is None else passing_percentage,
        show_explanations=show_explanations,
        status="PUBLISHED",
    )
    db.add(quiz)
    db.flush()

    single = QuizQuestion(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        question_type="SINGLE_CHOICE",
        question="Binary search complexity?",
        points=2.0,
        order_index=0,
        explanation="O(log n)",
    )
    single.options = [
        QuizOption(option_label="A", option_text="O(log n)", is_correct=True, order_index=0),
        QuizOption(option_label="B", option_text="O(n)", is_correct=False, order_index=1),
    ]

    multi = QuizQuestion(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        question_type="MULTIPLE_CHOICE",
        question="Select the HTTP methods.",
        points=3.0,
        order_index=1,
        explanation="GET and POST are HTTP methods.",
    )
    multi.options = [
        QuizOption(option_label="A", option_text="GET", is_correct=True, order_index=0),
        QuizOption(option_label="B", option_text="HTML", is_correct=False, order_index=1),
        QuizOption(option_label="C", option_text="POST", is_correct=True, order_index=2),
    ]

    fill = QuizQuestion(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        question_type="FILL_IN_BLANK",
        question="Which protocol secures HTTP?",
        points=1.0,
        order_index=2,
        explanation="HTTPS.",
        grading_config={
            FILL_IN_BLANK_ANSWERS_KEY: ["HTTPS", "HTTP over TLS"],
        },
    )

    db.add_all([single, multi, fill])
    db.flush()

    attempt = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        student_id=student.id,
        status=status,
        submitted_at=submitted_at if status == "SUBMITTED" else None,
    )
    db.add(attempt)
    db.flush()

    answers = [
        QuizAnswer(
            attempt_id=attempt.id,
            question_id=single.id,
            selected_option_labels=["A"],
        ),
        QuizAnswer(
            attempt_id=attempt.id,
            question_id=multi.id,
            selected_option_labels=["A", "C"],
        ),
        QuizAnswer(
            attempt_id=attempt.id,
            question_id=fill.id,
            text_answer="https",
        ),
    ]
    db.add_all(answers)
    db.commit()

    return {
        "trainer": trainer,
        "student": student,
        "class": clazz,
        "quiz": quiz,
        "single": single,
        "multi": multi,
        "fill": fill,
        "attempt": attempt,
        "answers": answers,
    }


def _answer_for(db: Session, attempt_id: uuid.UUID, question_id: uuid.UUID) -> QuizAnswer:
    return (
        db.query(QuizAnswer)
        .filter(
            QuizAnswer.attempt_id == attempt_id,
            QuizAnswer.question_id == question_id,
        )
        .one()
    )


def test_full_correct_attempt_snapshots_totals(db_session: Session) -> None:
    data = _seed_quiz(db_session)

    attempt = grade_attempt(db_session, data["attempt"].id)

    assert attempt.total_score == 6.0
    assert attempt.max_score == 6.0
    assert attempt.passing_percentage == 50.0
    assert attempt.passing_score == 3.0
    assert attempt.is_passed is True
    assert attempt.graded_at is not None
    assert attempt_percentage(attempt.total_score, attempt.max_score) == 100.0

    multi_answer = _answer_for(db_session, attempt.id, data["multi"].id)
    assert multi_answer.score_awarded == 3.0
    assert multi_answer.is_correct is True
    assert multi_answer.breakdown["fraction"] == 1.0


def test_partial_credit_and_wrong_answers(db_session: Session) -> None:
    data = _seed_quiz(db_session)
    data["answers"][0].selected_option_labels = ["B"]
    data["answers"][1].selected_option_labels = ["A"]
    data["answers"][2].text_answer = "not-https"
    db_session.commit()

    attempt = grade_attempt(db_session, data["attempt"].id)

    # Single 0 + multi partial 1.5 + fill 0 = 1.5 / 6 = 25%.
    assert attempt.total_score == 1.5
    assert attempt.is_passed is False
    assert attempt_percentage(attempt.total_score, attempt.max_score) == 25.0

    multi_answer = _answer_for(db_session, attempt.id, data["multi"].id)
    assert multi_answer.score_awarded == 1.5
    assert multi_answer.is_correct is False


def test_negative_multiple_choice_score_is_preserved(db_session: Session) -> None:
    data = _seed_quiz(db_session)
    data["answers"][0].selected_option_labels = ["B"]
    data["answers"][1].selected_option_labels = ["B"]
    data["answers"][2].text_answer = None
    db_session.commit()

    attempt = grade_attempt(db_session, data["attempt"].id)

    # Multi penalty with k=2 and one wrong option: -1.5.
    assert attempt.total_score == -1.5
    assert attempt.is_passed is False
    assert attempt_percentage(attempt.total_score, attempt.max_score) == -25.0

    multi_answer = _answer_for(db_session, attempt.id, data["multi"].id)
    assert multi_answer.score_awarded == -1.5
    assert multi_answer.breakdown["wrong_selected"] == ["B"]


def test_explicit_passing_percentage_is_snapshotted(db_session: Session) -> None:
    data = _seed_quiz(db_session, passing_percentage=80)
    data["answers"][1].selected_option_labels = ["A"]
    db_session.commit()

    attempt = grade_attempt(db_session, data["attempt"].id)

    assert attempt.total_score == 4.5
    assert attempt.passing_percentage == 80.0
    assert attempt.passing_score == 4.8
    assert attempt.is_passed is False


def test_total_equal_to_default_passing_score_passes(db_session: Session) -> None:
    data = _seed_quiz(db_session)
    data["answers"][0].selected_option_labels = ["B"]
    data["answers"][1].selected_option_labels = ["A", "C"]
    data["answers"][2].text_answer = None
    db_session.commit()

    attempt = grade_attempt(db_session, data["attempt"].id)

    assert attempt.total_score == 3.0
    assert attempt.passing_score == 3.0
    assert attempt.is_passed is True


def test_unanswered_question_is_materialised_with_zero(db_session: Session) -> None:
    data = _seed_quiz(db_session)
    db_session.delete(data["answers"][1])
    db_session.commit()

    attempt = grade_attempt(db_session, data["attempt"].id)

    assert attempt.total_score == 3.0  # 2 single + 0 missing + 1 fill
    missing_answer = _answer_for(db_session, attempt.id, data["multi"].id)
    assert missing_answer.score_awarded == 0.0
    assert missing_answer.is_correct is False
    assert missing_answer.selected_option_labels is None


def test_grading_is_idempotent(db_session: Session) -> None:
    data = _seed_quiz(db_session)

    first = grade_attempt(db_session, data["attempt"].id)
    graded_at = first.graded_at
    total = first.total_score
    answer_count = db_session.query(QuizAnswer).filter_by(attempt_id=first.id).count()

    second = grade_attempt(db_session, data["attempt"].id)

    assert second.graded_at == graded_at
    assert second.total_score == total
    assert db_session.query(QuizAnswer).filter_by(attempt_id=second.id).count() == answer_count


def test_in_progress_attempt_is_not_gradable(db_session: Session) -> None:
    data = _seed_quiz(db_session, status="IN_PROGRESS")

    with pytest.raises(AttemptNotGradableError):
        grade_attempt(db_session, data["attempt"].id)

    db_session.refresh(data["attempt"])
    assert data["attempt"].total_score is None
    assert data["attempt"].graded_at is None


@pytest.mark.parametrize(
    ("question_key", "labels"),
    [
        ("single", ["A", "B"]),
        ("single", ["A", "A"]),
        ("single", ["Z"]),
        ("multi", ["A", "A"]),
    ],
)
def test_invalid_answer_contract_leaves_attempt_retryable(
    db_session: Session, question_key: str, labels: list[str]
) -> None:
    data = _seed_quiz(db_session)
    answer = _answer_for(db_session, data["attempt"].id, data[question_key].id)
    answer.selected_option_labels = labels
    db_session.commit()

    with pytest.raises(AttemptAnswerValidationError):
        grade_attempt(db_session, data["attempt"].id)

    db_session.refresh(data["attempt"])
    assert data["attempt"].total_score is None
    assert data["attempt"].graded_at is None


def test_round_half_up_uses_decimal_rounding() -> None:
    assert round_half_up(0.025) == 0.03
    assert round_half_up(2.675) == 2.68
    assert round_half_up(-0.025) == -0.03
    assert round_half_up(4.444) == 4.44


def test_attempt_percentage_handles_missing_values() -> None:
    assert attempt_percentage(None, 6.0) is None
    assert attempt_percentage(3.0, None) is None
    assert attempt_percentage(3.0, 0.0) is None
    assert attempt_percentage(3.0, 6.0) == 50.0


def test_passing_boundary_uses_cross_multiplication() -> None:
    assert attempt_is_passed(3.0, 6.0, 50.0) is True
    assert attempt_is_passed(2.99, 6.0, 50.0) is False
    assert attempt_is_passed(3.0, 5.0, 60.0) is True
    assert attempt_is_passed(2.99, 5.0, 60.0) is False
    assert attempt_is_passed(0.0, 0.0, 50.0) is None


def test_select_official_attempts_highest_score_then_latest(db_session: Session) -> None:
    data = _seed_quiz(db_session)
    quiz = data["quiz"]
    student = data["student"]

    earlier = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        student_id=student.id,
        status="SUBMITTED",
        total_score=4.5,
        max_score=6.0,
        passing_score=3.0,
        is_passed=True,
        submitted_at=SUBMITTED_AT,
        graded_at=SUBMITTED_AT,
    )
    higher = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        student_id=student.id,
        status="SUBMITTED",
        total_score=5.0,
        max_score=6.0,
        passing_score=3.0,
        is_passed=True,
        submitted_at=SUBMITTED_AT + timedelta(minutes=5),
        graded_at=SUBMITTED_AT + timedelta(minutes=5),
    )
    latest_tie = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        student_id=student.id,
        status="SUBMITTED",
        total_score=5.0,
        max_score=6.0,
        passing_score=3.0,
        is_passed=True,
        submitted_at=SUBMITTED_AT + timedelta(minutes=10),
        graded_at=SUBMITTED_AT + timedelta(minutes=10),
    )
    db_session.add_all([earlier, higher, latest_tie])
    db_session.commit()

    official = select_official_attempts([earlier, higher, latest_tie])

    assert official[student.id].id == latest_tie.id


def test_select_official_attempts_is_per_student(db_session: Session) -> None:
    data = _seed_quiz(db_session)
    other_student = _create_user(
        db_session, "STUDENT", "student-2@example.com", "Second Student"
    )
    first_attempt = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=data["quiz"].id,
        student_id=data["student"].id,
        status="SUBMITTED",
        total_score=5.0,
        submitted_at=SUBMITTED_AT,
        graded_at=SUBMITTED_AT,
    )
    second_attempt = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=data["quiz"].id,
        student_id=other_student.id,
        status="SUBMITTED",
        total_score=6.0,
        submitted_at=SUBMITTED_AT,
        graded_at=SUBMITTED_AT,
    )
    db_session.add_all([first_attempt, second_attempt])
    db_session.commit()

    official = select_official_attempts([first_attempt, second_attempt])

    assert official[data["student"].id].id == first_attempt.id
    assert official[other_student.id].id == second_attempt.id
