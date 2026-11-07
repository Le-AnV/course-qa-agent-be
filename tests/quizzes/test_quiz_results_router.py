"""Router tests for AI-154 results, history, statistics and Excel export."""

from __future__ import annotations

import io
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.course import Class, ClassTrainer
from app.models.quiz import Quiz, QuizAnswer, QuizAttempt, QuizOption, QuizQuestion
from app.models.user import Role, User
from app.services.quiz_attempt_service import grade_attempt
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY

BASE_TIME = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture()
def db_session(monkeypatch) -> Generator[Session, None, None]:
    monkeypatch.setattr("app.services.reranker_service.warm_up", lambda: None)

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", "sqlite://")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dbmod._db_url = "sqlite://"
    Base.metadata.create_all(bind=engine)

    db = get_session_local("sqlite://")()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        reset_engine()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _headers(user_id: uuid.UUID, role: str) -> dict[str, str]:
    token = create_access_token(str(user_id), role)
    return {"Authorization": f"Bearer {token}"}


def _user(db: Session, role: Role, name: str, email: str) -> User:
    user = User(
        full_name=name,
        email=email,
        role_id=role.id,
        status="ACTIVE",
    )
    db.add(user)
    db.flush()
    return user


def _seed(db: Session) -> dict:
    # The app lifespan already seeds the static roles; reuse them when present.
    trainer_role = db.query(Role).filter_by(name="TRAINER").one_or_none()
    if trainer_role is None:
        trainer_role = Role(name="TRAINER")
        db.add(trainer_role)
    student_role = db.query(Role).filter_by(name="STUDENT").one_or_none()
    if student_role is None:
        student_role = Role(name="STUDENT")
        db.add(student_role)
    db.flush()

    trainer = _user(db, trainer_role, "Primary Trainer", "primary@example.com")
    co_trainer = _user(db, trainer_role, "Co Trainer", "co-trainer@example.com")
    other_trainer = _user(db, trainer_role, "Other Trainer", "other-trainer@example.com")
    student = _user(db, student_role, "Student One", "student-1@example.com")
    other_student = _user(db, student_role, "Student Two", "student-2@example.com")

    clazz = Class(
        trainer_id=trainer.id,
        code="AI154-A",
        name="AI-154 Class",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db.add(clazz)
    db.flush()
    db.add(ClassTrainer(class_id=clazz.id, trainer_id=co_trainer.id))

    quiz = Quiz(
        class_id=clazz.id,
        creator_id=trainer.id,
        title="Auto-grade Quiz",
        passing_score=None,
        show_explanations=False,
        status="PUBLISHED",
    )
    db.add(quiz)
    db.flush()

    single = QuizQuestion(
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
        quiz_id=quiz.id,
        question_type="FILL_IN_BLANK",
        question="Which protocol secures HTTP?",
        points=1.0,
        order_index=2,
        explanation="HTTPS.",
        grading_config={FILL_IN_BLANK_ANSWERS_KEY: ["HTTPS", "HTTP over TLS"]},
    )
    db.add_all([single, multi, fill])
    db.flush()

    def _attempt(
        student_id: uuid.UUID,
        started: datetime,
        submitted: datetime | None,
        status: str,
        answers: list[tuple[QuizQuestion, list[str] | None, str | None]],
    ) -> QuizAttempt:
        attempt = QuizAttempt(
            quiz_id=quiz.id,
            student_id=student_id,
            status=status,
            started_at=started,
            submitted_at=submitted,
        )
        db.add(attempt)
        db.flush()
        for question, labels, text in answers:
            db.add(
                QuizAnswer(
                    attempt_id=attempt.id,
                    question_id=question.id,
                    selected_option_labels=labels,
                    text_answer=text,
                )
            )
        db.flush()
        return attempt

    early = _attempt(
        student.id,
        BASE_TIME,
        BASE_TIME + timedelta(minutes=30),
        "SUBMITTED",
        [(single, ["B"], None), (multi, ["A", "C"], None), (fill, None, "wrong")],
    )
    late = _attempt(
        student.id,
        BASE_TIME + timedelta(hours=1),
        BASE_TIME + timedelta(minutes=90),
        "SUBMITTED",
        [(single, ["A"], None), (multi, ["A", "C"], None), (fill, None, "https")],
    )
    second = _attempt(
        other_student.id,
        BASE_TIME + timedelta(hours=1, minutes=10),
        BASE_TIME + timedelta(minutes=100),
        "SUBMITTED",
        [(single, ["A"], None), (multi, ["A"], None), (fill, None, "HTTPS")],
    )
    in_progress = _attempt(
        student.id,
        BASE_TIME + timedelta(hours=2),
        None,
        "IN_PROGRESS",
        [],
    )
    db.commit()

    grade_attempt(db, early.id)
    grade_attempt(db, late.id)
    grade_attempt(db, second.id)

    return {
        "trainer": trainer,
        "co_trainer": co_trainer,
        "other_trainer": other_trainer,
        "student": student,
        "other_student": other_student,
        "class": clazz,
        "quiz": quiz,
        "single": single,
        "multi": multi,
        "fill": fill,
        "early": early,
        "late": late,
        "second": second,
        "in_progress": in_progress,
    }


def _quiz_url(data: dict, suffix: str = "") -> str:
    return f"/api/classes/{data['class'].id}/quizzes/{data['quiz'].id}{suffix}"


def _student_url(data: dict, suffix: str = "") -> str:
    return f"/api/student/quizzes/{data['quiz'].id}{suffix}"


def test_student_history_marks_only_the_official_attempt(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)

    response = client.get(
        _student_url(data, "/attempts"),
        headers=_headers(data["student"].id, "student"),
    )

    assert response.status_code == 200
    rows = response.json()
    assert [row["status"] for row in rows] == ["IN_PROGRESS", "SUBMITTED", "SUBMITTED"]
    by_id = {row["id"]: row for row in rows}
    assert by_id[str(data["late"].id)]["official"] is True
    assert by_id[str(data["late"].id)]["total_score"] == 6.0
    assert by_id[str(data["late"].id)]["percentage"] == 100.0
    assert by_id[str(data["early"].id)]["official"] is False
    assert by_id[str(data["in_progress"].id)]["total_score"] is None
    assert by_id[str(data["in_progress"].id)]["official"] is False


def test_student_result_returns_scores_answers_and_hides_explanation(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)

    response = client.get(
        _student_url(data, f"/attempts/{data['late'].id}/result"),
        headers=_headers(data["student"].id, "student"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_score"] == 6.0
    assert body["max_score"] == 6.0
    assert body["percentage"] == 100.0
    assert body["is_passed"] is True
    assert body["official"] is True
    assert body["show_explanations"] is False
    assert len(body["questions"]) == 3

    questions = {q["id"]: q for q in body["questions"]}
    single = questions[str(data["single"].id)]
    assert single["answer"]["selected_option_labels"] == ["A"]
    assert single["answer"]["score_awarded"] == 2.0
    assert single["answer"]["is_correct"] is True
    assert single["correct_option_labels"] == ["A"]
    assert single["explanation"] is None
    assert {option["option_label"]: option["is_correct"] for option in single["options"]} == {
        "A": True,
        "B": False,
    }

    fill = questions[str(data["fill"].id)]
    assert fill["answer"]["text_answer"] == "https"
    assert fill["answer"]["is_correct"] is True
    assert set(fill["accepted_text_answers"]) == {"HTTPS", "HTTP over TLS"}


def test_student_result_shows_explanation_when_quiz_enables_it(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)
    data["quiz"].show_explanations = True
    db_session.commit()

    response = client.get(
        _student_url(data, f"/attempts/{data['late'].id}/result"),
        headers=_headers(data["student"].id, "student"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["show_explanations"] is True
    questions = {q["id"]: q for q in body["questions"]}
    assert questions[str(data["single"].id)]["explanation"] == "O(log n)"


def test_student_cannot_read_another_students_attempt(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)

    response = client.get(
        _student_url(data, f"/attempts/{data['late'].id}/result"),
        headers=_headers(data["other_student"].id, "student"),
    )

    assert response.status_code == 404


def test_student_result_rejects_unsubmitted_attempt(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)

    response = client.get(
        _student_url(data, f"/attempts/{data['in_progress'].id}/result"),
        headers=_headers(data["student"].id, "student"),
    )

    assert response.status_code == 409


def test_student_read_lazily_grades_a_submitted_attempt(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)
    attempt = QuizAttempt(
        quiz_id=data["quiz"].id,
        student_id=data["student"].id,
        status="SUBMITTED",
        started_at=BASE_TIME + timedelta(hours=3),
        submitted_at=BASE_TIME + timedelta(hours=3, minutes=10),
    )
    db_session.add(attempt)
    db_session.flush()
    for question, labels, text in (
        (data["single"], ["A"], None),
        (data["multi"], ["A", "C"], None),
        (data["fill"], None, "HTTPS"),
    ):
        db_session.add(
            QuizAnswer(
                attempt_id=attempt.id,
                question_id=question.id,
                selected_option_labels=labels,
                text_answer=text,
            )
        )
    db_session.commit()
    assert attempt.graded_at is None

    response = client.get(
        _student_url(data, f"/attempts/{attempt.id}/result"),
        headers=_headers(data["student"].id, "student"),
    )

    assert response.status_code == 200
    assert response.json()["total_score"] == 6.0
    db_session.refresh(attempt)
    assert attempt.graded_at is not None


def test_teacher_submissions_mark_official_attempts(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)

    response = client.get(
        _quiz_url(data, "/submissions"),
        headers=_headers(data["trainer"].id, "trainer"),
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 3
    assert [row["attempt_id"] for row in rows] == [
        str(data["early"].id),
        str(data["late"].id),
        str(data["second"].id),
    ]
    by_id = {row["attempt_id"]: row for row in rows}
    assert by_id[str(data["early"].id)]["official"] is False
    assert by_id[str(data["late"].id)]["official"] is True
    assert by_id[str(data["second"].id)]["official"] is True
    assert by_id[str(data["late"].id)]["student_name"] == "Student One"
    assert by_id[str(data["late"].id)]["percentage"] == 100.0


def test_teacher_statistics_use_one_official_attempt_per_student(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)

    response = client.get(
        _quiz_url(data, "/statistics"),
        headers=_headers(data["trainer"].id, "trainer"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["submitted_count"] == 2
    assert body["max_score"] == 6.0
    assert body["average_score"] == 5.25
    assert body["average_percentage"] == 87.5
    assert body["pass_rate"] == 100.0

    bands = {band["band"]: band["count"] for band in body["score_bands"]}
    assert bands == {"<50": 0, "50-59": 0, "60-69": 0, "70-79": 1, "80-89": 0, "90-100": 1}

    per_question = {row["question_id"]: row for row in body["per_question"]}
    single = per_question[str(data["single"].id)]
    assert single["answered_count"] == 2
    assert single["average_score"] == 2.0
    assert single["full_correct_rate"] == 100.0

    multi = per_question[str(data["multi"].id)]
    assert multi["answered_count"] == 2
    assert multi["average_score"] == 2.25
    assert multi["full_correct_rate"] == 50.0

    fill = per_question[str(data["fill"].id)]
    assert fill["average_score"] == 1.0
    assert fill["full_correct_rate"] == 100.0


def test_teacher_results_allow_co_trainer_and_reject_others(
    client: TestClient, db_session: Session
) -> None:
    data = _seed(db_session)

    co_trainer = client.get(
        _quiz_url(data, "/submissions"),
        headers=_headers(data["co_trainer"].id, "trainer"),
    )
    other_trainer = client.get(
        _quiz_url(data, "/submissions"),
        headers=_headers(data["other_trainer"].id, "trainer"),
    )
    student = client.get(
        _quiz_url(data, "/submissions"),
        headers=_headers(data["student"].id, "student"),
    )

    assert co_trainer.status_code == 200
    assert other_trainer.status_code == 403
    assert student.status_code == 403


def test_teacher_export_workbook_has_three_sheets(client: TestClient, db_session: Session) -> None:
    data = _seed(db_session)

    response = client.get(
        _quiz_url(data, "/results/export"),
        headers=_headers(data["trainer"].id, "trainer"),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MIME
    assert "results.xlsx" in response.headers["content-disposition"]

    workbook = openpyxl.load_workbook(io.BytesIO(response.content))
    assert workbook.sheetnames == ["Summary", "Submissions", "PerQuestion"]
    assert workbook["Summary"]["A3"].value == "Field"
    assert workbook["Summary"]["A4"].value == "Class"
    assert workbook["Summary"]["B4"].value == "AI154-A - AI-154 Class"
    assert workbook["Submissions"].max_row == 4  # header + three submitted attempts
    assert workbook["PerQuestion"].max_row == 4  # header + three questions


def test_missing_quiz_returns_404(client: TestClient, db_session: Session) -> None:
    data = _seed(db_session)
    unknown = uuid.uuid4().hex

    student = client.get(
        f"/api/student/quizzes/{unknown}/attempts",
        headers=_headers(data["student"].id, "student"),
    )
    teacher = client.get(
        f"/api/classes/{data['class'].id}/quizzes/{unknown}/submissions",
        headers=_headers(data["trainer"].id, "trainer"),
    )

    assert student.status_code == 404
    assert teacher.status_code == 404
