"""Router tests for the teacher quiz review, approval and export endpoints (AI-151)."""

from __future__ import annotations

import io
import uuid
from collections.abc import Generator

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
from app.models.course import Class
from app.models.quiz import Quiz
from app.models.user import Role, User
from app.schemas.quiz import QuizQuestionCreate
from app.services import quiz_generation_service
from app.services.quiz_excel import generate_quiz_template_workbook, validate_and_parse_quiz_excel
from app.services.quiz_validation import GeneratedQuestion

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture()
def db_session(monkeypatch) -> Generator[Session, None, None]:
    # These tests never exercise the external reranking model.
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
    trainer_role = Role(name="TRAINER")
    student_role = Role(name="STUDENT")
    db.add_all([trainer_role, student_role])
    db.flush()

    trainer = User(
        full_name="Nguyen Chi Trung",
        email="review-trainer@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    other_trainer = User(
        full_name="Other Trainer",
        email="review-other-trainer@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    student = User(
        full_name="Student",
        email="review-student@example.com",
        role_id=student_role.id,
        status="ACTIVE",
    )
    db.add_all([trainer, other_trainer, student])
    db.flush()

    clazz = Class(
        trainer_id=trainer.id,
        code="AI151-A",
        name="AI-151 Class",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    other_class = Class(
        trainer_id=other_trainer.id,
        code="AI151-B",
        name="Other Class",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db.add_all([clazz, other_class])
    db.commit()
    db.info.update(
        trainer_id=trainer.id,
        other_trainer_id=other_trainer.id,
        student_id=student.id,
        class_id=clazz.id,
        other_class_id=other_class.id,
    )
    try:
        yield db
    finally:
        quiz_generation_service.set_regenerator(None)
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


def _headers(user_id: uuid.UUID, role: str = "trainer") -> dict[str, str]:
    token = create_access_token(str(user_id), role)
    return {"Authorization": f"Bearer {token}"}


def _template_bytes() -> bytes:
    stream = io.BytesIO()
    generate_quiz_template_workbook().save(stream)
    return stream.getvalue()


def _quizzes_url(db_session: Session, class_key: str = "class_id") -> str:
    return f"/api/classes/{db_session.info[class_key]}/quizzes"


def _import_quiz(client: TestClient, db_session: Session) -> dict:
    response = client.post(
        f"{_quizzes_url(db_session)}/import",
        headers=_headers(db_session.info["trainer_id"]),
        data={"title": "Review Quiz", "description": "Chapter 1"},
        files={"file": ("quiz.xlsx", _template_bytes(), XLSX_MIME)},
    )
    assert response.status_code == 201
    return response.json()


def _quiz_url(db_session: Session, quiz_id: str, suffix: str = "") -> str:
    return f"{_quizzes_url(db_session)}/{quiz_id}{suffix}"


def _sample_question(**overrides) -> dict:
    payload = {
        "question_type": "SINGLE_CHOICE",
        "question": "Which layer routes packets?",
        "points": 2.0,
        "difficulty": "APPLY",
        "options": [
            {"option_label": "A", "option_text": "Network", "is_correct": True},
            {"option_label": "B", "option_text": "Physical", "is_correct": False},
        ],
    }
    payload.update(overrides)
    return payload


def test_patch_quiz_metadata_updates_only_sent_fields(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)

    response = client.patch(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
        json={"title": "  Renamed Quiz  ", "time_limit_minutes": 45},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Renamed Quiz"
    assert body["time_limit_minutes"] == 45
    assert body["description"] == "Chapter 1"
    assert body["passing_score"] is None


def test_patch_quiz_rejects_non_draft(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)
    client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    response = client.patch(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
        json={"title": "Too late"},
    )

    assert response.status_code == 409
    assert "draft" in response.json()["detail"].lower()


def test_quiz_read_includes_totals(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)

    response = client.get(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 200
    body = response.json()
    expected_total = round(sum(q["points"] for q in body["questions"]), 2)
    assert body["question_count"] == 3
    assert body["total_points"] == expected_total
    assert sum(body["question_type_counts"].values()) == 3
    assert body["approved_at"] is None
    assert body["approved_by_name"] is None


def test_add_question_appends_with_next_order_index(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)

    response = client.post(
        _quiz_url(db_session, imported["id"], "/questions"),
        headers=_headers(db_session.info["trainer_id"]),
        json=_sample_question(),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["question_count"] == 4
    added = body["questions"][-1]
    assert added["order_index"] == 3
    assert added["question"] == "Which layer routes packets?"
    assert [o["option_label"] for o in added["options"]] == ["A", "B"]


def test_add_question_rejects_invalid_choice_shape(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)

    response = client.post(
        _quiz_url(db_session, imported["id"], "/questions"),
        headers=_headers(db_session.info["trainer_id"]),
        json=_sample_question(
            options=[
                {"option_label": "A", "option_text": "One", "is_correct": True},
                {"option_label": "B", "option_text": "Two", "is_correct": True},
            ]
        ),
    )

    assert response.status_code == 422


def test_replace_question_preserves_id_and_order_index(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    original = imported["questions"][0]

    response = client.put(
        _quiz_url(db_session, imported["id"], f"/questions/{original['id']}"),
        headers=_headers(db_session.info["trainer_id"]),
        json=_sample_question(points=5.0),
    )

    assert response.status_code == 200
    body = response.json()
    replaced = next(q for q in body["questions"] if q["id"] == original["id"])
    assert replaced["order_index"] == original["order_index"]
    assert replaced["points"] == 5.0
    assert replaced["question"] == "Which layer routes packets?"
    assert body["total_points"] == round(
        sum(q["points"] for q in body["questions"]), 2
    )


def test_replace_question_404_when_from_another_quiz(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    original = imported["questions"][0]

    response = client.put(
        _quiz_url(db_session, imported["id"], f"/questions/{uuid.uuid4()}"),
        headers=_headers(db_session.info["trainer_id"]),
        json=_sample_question(),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Question not found."
    assert original["id"] != str(uuid.uuid4())


def test_delete_question_resequences_order_index(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    removed = imported["questions"][1]

    response = client.delete(
        _quiz_url(db_session, imported["id"], f"/questions/{removed['id']}"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["question_count"] == 2
    assert [q["order_index"] for q in body["questions"]] == [0, 1]
    assert all(q["id"] != removed["id"] for q in body["questions"])


def _install_regenerator(result=None, error=None):
    def _stub(**kwargs):
        if error is not None:
            raise error
        return result

    quiz_generation_service.set_regenerator(_stub)


def test_regenerate_replaces_in_place(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]
    generated = QuizQuestionCreate(
        **_sample_question(question="Regenerated prompt", points=target["points"])
    )
    _install_regenerator(result=generated)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={"instructions": "Make it harder"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["question_count"] == 3
    replaced = next(q for q in body["questions"] if q["id"] == target["id"])
    assert replaced["question"] == "Regenerated prompt"
    assert replaced["order_index"] == target["order_index"]


def test_regenerate_returns_503_when_generator_unavailable(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 503
    refreshed = client.get(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    ).json()
    kept = next(q for q in refreshed["questions"] if q["id"] == target["id"])
    assert kept["question"] == target["question"]


def test_regenerate_keeps_original_on_invalid_output(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]
    _install_regenerator(error=ValueError("bad generator output"))

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 502
    refreshed = client.get(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    ).json()
    kept = next(q for q in refreshed["questions"] if q["id"] == target["id"])
    assert kept["question"] == target["question"]


def test_approve_sets_approver_and_timestamp(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)

    response = client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PUBLISHED"
    assert body["approved_by_id"] == str(db_session.info["trainer_id"])
    assert body["approved_by_name"] == "Nguyen Chi Trung"
    assert body["approved_at"] is not None


def test_approve_rejects_empty_quiz(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)
    for question in imported["questions"]:
        client.delete(
            _quiz_url(db_session, imported["id"], f"/questions/{question['id']}"),
            headers=_headers(db_session.info["trainer_id"]),
        )

    response = client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 409
    assert "at least one question" in response.json()["detail"]


def test_approve_is_not_idempotent(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)
    first = client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )
    assert first.status_code == 200

    second = client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert second.status_code == 409


def test_reopen_clears_approval(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)
    client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    response = client.post(
        _quiz_url(db_session, imported["id"], "/reopen"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["approved_by_id"] is None
    assert body["approved_at"] is None
    assert body["approved_by_name"] is None


def test_export_blocks_unapproved_quiz(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)

    response = client.get(
        _quiz_url(db_session, imported["id"], "/export"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Approve this quiz before exporting it."


def test_export_returns_ai148_workbook(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)
    client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    response = client.get(
        _quiz_url(db_session, imported["id"], "/export"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MIME
    assert response.content.startswith(b"PK")
    assert "attachment;" in response.headers["content-disposition"]
    assert response.content is not None and len(response.content) > 0

    wb = openpyxl.load_workbook(io.BytesIO(response.content))
    assert "Questions" in wb.sheetnames


def test_export_reimports_without_schema_drift(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )
    exported = client.get(
        _quiz_url(db_session, imported["id"], "/export"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    result = validate_and_parse_quiz_excel(exported.content)

    assert result.is_valid is True, result.errors
    assert len(result.questions) == len(imported["questions"])
    for original, reimported in zip(imported["questions"], result.questions):
        assert original["question"] == reimported.question
        assert original["question_type"] == reimported.question_type
        assert float(original["points"]) == float(reimported.points)
        assert original["difficulty"] == reimported.difficulty


def test_review_routes_require_class_teacher(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)
    question = imported["questions"][0]
    url = _quiz_url(db_session, imported["id"])

    student_headers = _headers(db_session.info["student_id"], "student")
    other_headers = _headers(db_session.info["other_trainer_id"])

    assert client.patch(url, headers=student_headers, json={"title": "x"}).status_code == 403
    assert (
        client.post(f"{url}/questions", headers=student_headers, json=_sample_question()).status_code
        == 403
    )
    assert client.post(f"{url}/approve", headers=student_headers).status_code == 403
    assert client.get(f"{url}/export", headers=student_headers).status_code == 403
    assert client.post(f"{url}/approve", headers=other_headers).status_code == 403
    assert (
        client.put(
            f"{url}/questions/{question['id']}",
            headers=other_headers,
            json=_sample_question(),
        ).status_code
        == 403
    )


def test_quiz_from_another_class_is_404(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)

    response = client.get(
        f"/api/classes/{db_session.info['other_class_id']}/quizzes/{imported['id']}",
        headers=_headers(db_session.info["other_trainer_id"]),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Quiz not found."


def test_delete_draft_quiz(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)

    response = client.delete(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.query(Quiz).count() == 0


def test_delete_approved_quiz_is_rejected(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)
    client.post(
        _quiz_url(db_session, imported["id"], "/approve"),
        headers=_headers(db_session.info["trainer_id"]),
    )

    response = client.delete(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# AI-150: bounded regeneration and validation
# ---------------------------------------------------------------------------


def test_regenerate_retries_invalid_output_before_success(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]
    calls = 0

    def _stub(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("first attempt invalid")
        return QuizQuestionCreate(**_sample_question(question="Second attempt accepted"))

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "DRAFT"
    replaced = next(q for q in body["questions"] if q["id"] == target["id"])
    assert replaced["question"] == "Second attempt accepted"
    assert calls == 2


def test_regenerate_exhausts_attempts_and_preserves_original(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]
    calls = 0

    def _stub(**kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("always invalid")

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 502
    assert calls == quiz_generation_service.MAX_REGENERATION_ATTEMPTS
    refreshed = client.get(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    ).json()
    kept = next(q for q in refreshed["questions"] if q["id"] == target["id"])
    assert kept["question"] == target["question"]


def test_regenerate_passes_previous_failure_reason_to_next_attempt(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]
    instructions_seen: list[str | None] = []

    def _stub(**kwargs):
        instructions_seen.append(kwargs["instructions"])
        if len(instructions_seen) == 1:
            raise ValueError("first attempt invalid")
        return QuizQuestionCreate(**_sample_question(question="Accepted after feedback"))

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={"instructions": "Make it harder"},
    )

    assert response.status_code == 200
    assert len(instructions_seen) == 2
    assert instructions_seen[0] == "Make it harder"
    assert "first attempt invalid" in (instructions_seen[1] or "")


def test_regenerate_rejects_duplicate_and_preserves_original(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]
    sibling = imported["questions"][1]

    def _stub(**kwargs):
        return QuizQuestionCreate(
            **_sample_question(question=sibling["question"])
        )

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 502
    refreshed = client.get(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    ).json()
    kept = next(q for q in refreshed["questions"] if q["id"] == target["id"])
    assert kept["question"] == target["question"]


def test_regenerate_timeout_is_bounded_and_preserves_original(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]
    calls = 0

    def _stub(**kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("generator timed out")

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 502
    assert calls == quiz_generation_service.MAX_REGENERATION_ATTEMPTS
    refreshed = client.get(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    ).json()
    kept = next(q for q in refreshed["questions"] if q["id"] == target["id"])
    assert kept["question"] == target["question"]


def test_regenerate_malformed_payload_is_rejected_and_preserves_original(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]
    calls = 0

    def _stub(**kwargs):
        nonlocal calls
        calls += 1
        return {"question": "partial payload"}  # type: ignore[return-value]

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 502
    assert calls == quiz_generation_service.MAX_REGENERATION_ATTEMPTS
    refreshed = client.get(
        _quiz_url(db_session, imported["id"]),
        headers=_headers(db_session.info["trainer_id"]),
    ).json()
    kept = next(q for q in refreshed["questions"] if q["id"] == target["id"])
    assert kept["question"] == target["question"]


def test_regenerate_persists_low_confidence_flag(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]

    def _stub(**kwargs):
        return GeneratedQuestion(
            data=QuizQuestionCreate(
                **_sample_question(question="Low confidence but valid")
            ),
            confidence=0.4,
        )

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 200
    body = response.json()
    replaced = next(q for q in body["questions"] if q["id"] == target["id"])
    assert replaced["confidence"] == 0.4
    assert replaced["validation_flags"] == ["LOW_CONFIDENCE"]


@pytest.mark.parametrize("confidence", [True, "0.4", float("nan")])
def test_regenerate_malformed_confidence_is_not_persisted(
    client: TestClient, db_session: Session, confidence: object
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]

    def _stub(**kwargs):
        return GeneratedQuestion(
            data=QuizQuestionCreate(
                **_sample_question(question="Malformed confidence replacement")
            ),
            confidence=confidence,
        )

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 200
    replaced = next(
        q for q in response.json()["questions"] if q["id"] == target["id"]
    )
    assert replaced["confidence"] is None
    assert replaced["validation_flags"] == []


def test_regenerate_persists_fill_in_blank_answers_in_grading_config(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = next(
        question
        for question in imported["questions"]
        if question["question_type"] == "FILL_IN_BLANK"
    )

    def _stub(**kwargs):
        return QuizQuestionCreate(
            question_type="FILL_IN_BLANK",
            question="Name the capital of France.",
            points=target["points"],
            difficulty=target["difficulty"],
            correct_answers=["Paris", "City of Paris"],
        )

    quiz_generation_service.set_regenerator(_stub)

    response = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )

    assert response.status_code == 200
    body = response.json()
    replaced = next(q for q in body["questions"] if q["id"] == target["id"])
    assert replaced["grading_config"]["correct_answers"] == ["Paris", "City of Paris"]


def test_manual_add_ignores_forged_validation_metadata(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    payload = {
        **_sample_question(question="Forged metadata on add"),
        "confidence": 0.99,
        "validation_flags": ["LOW_CONFIDENCE"],
    }

    response = client.post(
        _quiz_url(db_session, imported["id"], "/questions"),
        headers=_headers(db_session.info["trainer_id"]),
        json=payload,
    )

    assert response.status_code == 201
    added = response.json()["questions"][-1]
    assert added["question"] == "Forged metadata on add"
    assert added["confidence"] is None
    assert added["validation_flags"] == []


def test_manual_edit_preserves_trusted_validation_metadata(
    client: TestClient, db_session: Session
) -> None:
    imported = _import_quiz(client, db_session)
    target = imported["questions"][0]

    def _stub(**kwargs):
        return GeneratedQuestion(
            data=QuizQuestionCreate(
                **_sample_question(question="Low confidence replacement")
            ),
            confidence=0.4,
        )

    quiz_generation_service.set_regenerator(_stub)
    regenerated = client.post(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}/regenerate"),
        headers=_headers(db_session.info["trainer_id"]),
        json={},
    )
    assert regenerated.status_code == 200
    assert next(
        q for q in regenerated.json()["questions"] if q["id"] == target["id"]
    )["validation_flags"] == ["LOW_CONFIDENCE"]

    response = client.put(
        _quiz_url(db_session, imported["id"], f"/questions/{target['id']}"),
        headers=_headers(db_session.info["trainer_id"]),
        json={
            **_sample_question(question="Teacher-edited prompt"),
            "confidence": 0.99,
            "validation_flags": [],
        },
    )

    assert response.status_code == 200
    replaced = next(q for q in response.json()["questions"] if q["id"] == target["id"])
    assert replaced["question"] == "Teacher-edited prompt"
    assert replaced["confidence"] == 0.4
    assert replaced["validation_flags"] == ["LOW_CONFIDENCE"]
