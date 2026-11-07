"""Router tests for student quiz delivery (AI-153)."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.course import Class, Enrollment
from app.models.quiz import Quiz, QuizAttempt, QuizOption, QuizQuestion
from app.models.user import Role, User


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
    student_role = Role(name="STUDENT")
    trainer_role = Role(name="TRAINER")
    db.add_all([student_role, trainer_role])
    db.flush()

    trainer = User(
        full_name="Trainer",
        email="ai153-trainer@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    student = User(
        full_name="Student",
        email="ai153-student@example.com",
        role_id=student_role.id,
        status="ACTIVE",
    )
    other_student = User(
        full_name="Other Student",
        email="ai153-other@example.com",
        role_id=student_role.id,
        status="ACTIVE",
    )
    outsider = User(
        full_name="Outsider",
        email="ai153-outsider@example.com",
        role_id=student_role.id,
        status="ACTIVE",
    )
    db.add_all([trainer, student, other_student, outsider])
    db.flush()

    clazz = Class(
        trainer_id=trainer.id,
        code="AI153-A",
        name="AI-153 Class",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db.add(clazz)
    db.flush()
    db.add_all(
        [
            Enrollment(class_id=clazz.id, student_id=student.id, status="ACTIVE"),
            Enrollment(class_id=clazz.id, student_id=other_student.id, status="ACTIVE"),
        ]
    )

    now = datetime.now(UTC)
    quiz = Quiz(
        class_id=clazz.id,
        creator_id=trainer.id,
        title="Timed Quiz",
        description="Midterm",
        time_limit_minutes=30,
        passing_score=60,
        status="PUBLISHED",
        opens_at=now - timedelta(hours=1),
        closes_at=now + timedelta(hours=1),
        max_attempts=2,
        questions=[
            QuizQuestion(
                question_type="SINGLE_CHOICE",
                question="Which layer routes packets?",
                points=2,
                difficulty="APPLY",
                order_index=0,
                explanation="The network layer routes packets.",
                rubric="Network layer",
                source_reference="Slide 4",
                options=[
                    QuizOption(option_label="A", option_text="Network", is_correct=True),
                    QuizOption(option_label="B", option_text="Physical", is_correct=False),
                ],
            ),
            QuizQuestion(
                question_type="MULTIPLE_CHOICE",
                question="Select the transport protocols.",
                points=2,
                difficulty="UNDERSTAND",
                order_index=1,
                explanation="TCP and UDP are transport protocols.",
                options=[
                    QuizOption(option_label="A", option_text="TCP", is_correct=True),
                    QuizOption(option_label="B", option_text="IP", is_correct=False),
                    QuizOption(option_label="C", option_text="UDP", is_correct=True),
                ],
            ),
            QuizQuestion(
                question_type="FILL_IN_BLANK",
                question="TLS runs on top of ____.",
                points=1,
                difficulty="REMEMBER",
                order_index=2,
                grading_config={"correct_answers": ["HTTPS", "HTTP over TLS"]},
            ),
        ],
    )
    upcoming = Quiz(
        class_id=clazz.id,
        creator_id=trainer.id,
        title="Upcoming Quiz",
        status="PUBLISHED",
        opens_at=now + timedelta(hours=1),
        closes_at=now + timedelta(hours=2),
        max_attempts=1,
    )
    closed = Quiz(
        class_id=clazz.id,
        creator_id=trainer.id,
        title="Closed Quiz",
        status="PUBLISHED",
        opens_at=now - timedelta(hours=2),
        closes_at=now - timedelta(hours=1),
        max_attempts=1,
    )
    draft = Quiz(
        class_id=clazz.id,
        creator_id=trainer.id,
        title="Draft Quiz",
        status="DRAFT",
    )
    one_shot = Quiz(
        class_id=clazz.id,
        creator_id=trainer.id,
        title="One Shot Quiz",
        status="PUBLISHED",
        max_attempts=1,
        questions=[
            QuizQuestion(
                question_type="SINGLE_CHOICE",
                question="2 + 2 = ?",
                points=1,
                order_index=0,
                options=[
                    QuizOption(option_label="A", option_text="4", is_correct=True),
                    QuizOption(option_label="B", option_text="5", is_correct=False),
                ],
            )
        ],
    )
    db.add_all([quiz, upcoming, closed, draft, one_shot])
    db.commit()
    db.info.update(
        trainer_id=trainer.id,
        student_id=student.id,
        other_student_id=other_student.id,
        outsider_id=outsider.id,
        class_id=clazz.id,
        quiz_id=quiz.id,
        upcoming_id=upcoming.id,
        closed_id=closed.id,
        draft_id=draft.id,
        one_shot_id=one_shot.id,
    )
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


def _headers(user_id: uuid.UUID, role: str = "student") -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id), role)}"}


def _start(client: TestClient, db_session: Session, quiz_key: str = "quiz_id") -> dict:
    response = client.post(
        f"/api/student/quizzes/{db_session.info[quiz_key]}/attempts",
        headers=_headers(db_session.info["student_id"]),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _answer(question_id: str, labels: list[str] | None = None, text: str | None = None) -> dict:
    return {
        "question_id": question_id,
        "selected_option_labels": labels,
        "text_answer": text,
    }


def test_list_shows_only_published_enrolled_quizzes_with_window_and_attempts(
    client: TestClient, db_session: Session
) -> None:
    response = client.get(
        "/api/student/quizzes", headers=_headers(db_session.info["student_id"])
    )
    assert response.status_code == 200
    body = response.json()
    assert body["server_time"]
    by_title = {row["title"]: row for row in body["quizzes"]}

    assert set(by_title) == {"Timed Quiz", "Upcoming Quiz", "Closed Quiz", "One Shot Quiz"}
    assert by_title["Timed Quiz"]["availability"] == "OPEN"
    assert by_title["Timed Quiz"]["attempts_left"] == 2
    assert by_title["Timed Quiz"]["max_attempts"] == 2
    assert by_title["Timed Quiz"]["class_code"] == "AI153-A"
    assert by_title["Upcoming Quiz"]["availability"] == "UPCOMING"
    assert by_title["Closed Quiz"]["availability"] == "CLOSED"
    assert by_title["Closed Quiz"]["can_start"] is False
    assert by_title["Timed Quiz"]["can_start"] is True

    outsider = client.get(
        "/api/student/quizzes", headers=_headers(db_session.info["outsider_id"])
    )
    assert outsider.status_code == 200
    assert outsider.json()["quizzes"] == []


def test_exam_payload_hides_answers_explanations_and_rubrics(
    client: TestClient, db_session: Session
) -> None:
    session = _start(client, db_session)
    assert session["status"] == "IN_PROGRESS"
    assert session["deadline_at"] is not None
    assert session["remaining_seconds"] > 0
    assert session["total_points"] == 5.0
    assert session["answers"] == []

    serialized = str(session["questions"])
    for forbidden in ("is_correct", "explanation", "rubric", "source_reference", "grading_config"):
        assert forbidden not in serialized
    for question in session["questions"]:
        for option in question["options"]:
            assert set(option) == {"id", "option_label", "option_text", "order_index"}


def test_start_before_open_after_close_and_unpublished_are_rejected(
    client: TestClient, db_session: Session
) -> None:
    headers = _headers(db_session.info["student_id"])
    for key in ("upcoming_id", "closed_id"):
        response = client.post(
            f"/api/student/quizzes/{db_session.info[key]}/attempts", headers=headers
        )
        assert response.status_code == 409, response.text

    draft = client.post(
        f"/api/student/quizzes/{db_session.info['draft_id']}/attempts", headers=headers
    )
    assert draft.status_code == 404


def test_autosave_upserts_reloads_and_clears_answers(
    client: TestClient, db_session: Session
) -> None:
    headers = _headers(db_session.info["student_id"])
    session = _start(client, db_session)
    questions = {question["order_index"]: question for question in session["questions"]}
    single = questions[0]["id"]
    multi = questions[1]["id"]

    saved = client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={
            "answers": [
                {**_answer(single, ["A"]), "is_marked_for_review": True},
                _answer(multi, ["A", "C"]),
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["answered_count"] == 2

    reloaded = client.get(
        f"/api/student/attempts/{session['attempt_id']}", headers=headers
    )
    assert reloaded.status_code == 200
    answers = {answer["question_id"]: answer for answer in reloaded.json()["answers"]}
    assert answers[single]["selected_option_labels"] == ["A"]
    assert answers[single]["is_marked_for_review"] is True
    assert answers[multi]["selected_option_labels"] == ["A", "C"]

    cleared = client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={"answers": [_answer(multi, None, None)]},
    )
    assert cleared.status_code == 200
    remaining = {answer["question_id"] for answer in cleared.json()["answers"]}
    assert multi not in remaining
    assert single in remaining


def test_submit_grades_once_and_reveals_solutions(
    client: TestClient, db_session: Session
) -> None:
    headers = _headers(db_session.info["student_id"])
    session = _start(client, db_session)
    questions = {question["order_index"]: question for question in session["questions"]}

    payload = {
        "answers": [
            _answer(questions[0]["id"], ["A"]),
            _answer(questions[1]["id"], ["A", "C"]),
            _answer(questions[2]["id"], None, "https"),
        ]
    }
    first = client.post(
        f"/api/student/attempts/{session['attempt_id']}/submit",
        headers=headers,
        json=payload,
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "SUBMITTED"
    assert body["result"]["total_score"] == 5.0
    assert body["result"]["percentage"] == 100.0
    assert body["result"]["is_passed"] is True
    assert body["result"]["questions"][0]["options"][0]["is_correct"] is True
    assert body["result"]["questions"][0]["explanation"]

    # AC-06: a second click returns the same attempt, not a second submission.
    second = client.post(
        f"/api/student/attempts/{session['attempt_id']}/submit",
        headers=headers,
        json=payload,
    )
    assert second.status_code == 200
    assert second.json()["attempt_id"] == body["attempt_id"]
    assert second.json()["result"]["total_score"] == body["result"]["total_score"]
    count = db_session.execute(
        select(func.count()).select_from(QuizAttempt).where(
            QuizAttempt.quiz_id == db_session.info["quiz_id"],
            QuizAttempt.student_id == db_session.info["student_id"],
        )
    ).scalar_one()
    assert count == 1

    # Draft autosave after submission is refused.
    rejected = client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={"answers": [_answer(questions[0]["id"], ["B"])]},
    )
    assert rejected.status_code == 409


def test_max_attempts_is_enforced(client: TestClient, db_session: Session) -> None:
    headers = _headers(db_session.info["student_id"])
    session = _start(client, db_session, "one_shot_id")
    another = client.post(
        f"/api/student/quizzes/{db_session.info['one_shot_id']}/attempts", headers=headers
    )
    # Resuming the active attempt is allowed even when the limit is reached.
    assert another.status_code == 200
    assert another.json()["attempt_id"] == session["attempt_id"]

    client.post(
        f"/api/student/attempts/{session['attempt_id']}/submit", headers=headers, json={"answers": []}
    )
    blocked = client.post(
        f"/api/student/quizzes/{db_session.info['one_shot_id']}/attempts", headers=headers
    )
    assert blocked.status_code == 409
    assert "No attempts left" in blocked.text


def test_timeout_auto_submits_and_saves_are_refused(
    client: TestClient, db_session: Session
) -> None:
    headers = _headers(db_session.info["student_id"])
    session = _start(client, db_session)

    attempt = db_session.get(QuizAttempt, uuid.UUID(session["attempt_id"]))
    assert attempt is not None
    attempt.started_at = datetime.now(UTC) - timedelta(minutes=45)
    db_session.commit()

    reloaded = client.get(
        f"/api/student/attempts/{session['attempt_id']}", headers=headers
    )
    assert reloaded.status_code == 200
    body = reloaded.json()
    assert body["status"] == "EXPIRED"
    assert body["questions"] == []
    assert body["result"]["status"] == "EXPIRED"
    assert body["result"]["submitted_at"] is not None
    assert body["remaining_seconds"] == 0

    late_save = client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={"answers": []},
    )
    assert late_save.status_code == 409


def test_another_student_cannot_read_or_write_the_attempt(
    client: TestClient, db_session: Session
) -> None:
    session = _start(client, db_session)
    headers = _headers(db_session.info["other_student_id"])

    assert (
        client.get(f"/api/student/attempts/{session['attempt_id']}", headers=headers).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/student/attempts/{session['attempt_id']}/answers",
            headers=headers,
            json={"answers": []},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/student/attempts/{session['attempt_id']}/submit",
            headers=headers,
            json={"answers": []},
        ).status_code
        == 404
    )


def test_invalid_answer_payload_is_rejected(client: TestClient, db_session: Session) -> None:
    headers = _headers(db_session.info["student_id"])
    session = _start(client, db_session)
    single = session["questions"][0]["id"]
    fill = session["questions"][2]["id"]

    many = client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={"answers": [_answer(single, ["A", "B"])]},
    )
    assert many.status_code == 422

    text_on_choice = client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={"answers": [_answer(single, None, "A")]},
    )
    assert text_on_choice.status_code == 422

    option_on_fill = client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={"answers": [_answer(fill, ["A"])]},
    )
    assert option_on_fill.status_code == 422

    unknown = client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={"answers": [_answer(str(uuid.uuid4()), ["A"])]},
    )
    assert unknown.status_code == 422


def test_submit_with_a_cleared_answer_does_not_score_the_stale_row(
    client: TestClient, db_session: Session
) -> None:
    headers = _headers(db_session.info["student_id"])
    session = _start(client, db_session)
    questions = {question["order_index"]: question for question in session["questions"]}
    single = questions[0]["id"]
    multi = questions[1]["id"]
    fill = questions[2]["id"]

    client.patch(
        f"/api/student/attempts/{session['attempt_id']}/answers",
        headers=headers,
        json={"answers": [_answer(single, ["A"])]},
    )
    submitted = client.post(
        f"/api/student/attempts/{session['attempt_id']}/submit",
        headers=headers,
        json={
            "answers": [
                _answer(single, None, None),
                _answer(multi, ["A", "C"]),
                _answer(fill, None, "HTTPS"),
            ]
        },
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["result"]["total_score"] == 3.0
    assert {answer["question_id"] for answer in body["result"]["answers"]} == {multi, fill}


def test_starting_twice_resumes_the_same_active_attempt(
    client: TestClient, db_session: Session
) -> None:
    first = _start(client, db_session)
    second = _start(client, db_session)
    assert second["attempt_id"] == first["attempt_id"]
    count = db_session.execute(
        select(func.count()).select_from(QuizAttempt).where(
            QuizAttempt.quiz_id == db_session.info["quiz_id"],
            QuizAttempt.student_id == db_session.info["student_id"],
        )
    ).scalar_one()
    assert count == 1


def test_teacher_sets_window_and_attempt_limit(
    client: TestClient, db_session: Session
) -> None:
    headers = _headers(db_session.info["trainer_id"], "trainer")
    draft_id = db_session.info["draft_id"]
    opens = datetime.now(UTC) + timedelta(hours=1)
    closes = opens + timedelta(hours=2)

    updated = client.patch(
        f"/api/classes/{db_session.info['class_id']}/quizzes/{draft_id}",
        headers=headers,
        json={
            "opens_at": opens.isoformat(),
            "closes_at": closes.isoformat(),
            "max_attempts": 3,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["max_attempts"] == 3
    assert body["opens_at"] is not None

    invalid = client.patch(
        f"/api/classes/{db_session.info['class_id']}/quizzes/{draft_id}",
        headers=headers,
        json={"opens_at": (closes + timedelta(hours=1)).isoformat()},
    )
    assert invalid.status_code == 409

    cleared = client.patch(
        f"/api/classes/{db_session.info['class_id']}/quizzes/{draft_id}",
        headers=headers,
        json={"max_attempts": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["max_attempts"] is None
