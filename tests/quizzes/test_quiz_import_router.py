"""Router tests for the teacher quiz Excel import endpoints (AI-152)."""

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
from app.services.quiz_excel import (
    FILL_IN_BLANK_ANSWERS_KEY,
    generate_quiz_template_workbook,
)

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
        full_name="Trainer",
        email="quiz-trainer@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    other_trainer = User(
        full_name="Other Trainer",
        email="quiz-other-trainer@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    student = User(
        full_name="Student",
        email="quiz-student@example.com",
        role_id=student_role.id,
        status="ACTIVE",
    )
    db.add_all([trainer, other_trainer, student])
    db.flush()

    clazz = Class(
        trainer_id=trainer.id,
        code="AI152-A",
        name="AI-152 Class",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db.add(clazz)
    db.commit()
    db.info.update(
        trainer_id=trainer.id,
        other_trainer_id=other_trainer.id,
        student_id=student.id,
        class_id=clazz.id,
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


def _headers(user_id: uuid.UUID, role: str = "trainer") -> dict[str, str]:
    token = create_access_token(str(user_id), role)
    return {"Authorization": f"Bearer {token}"}


def _template_bytes() -> bytes:
    stream = io.BytesIO()
    generate_quiz_template_workbook().save(stream)
    return stream.getvalue()


def _invalid_template_bytes() -> bytes:
    wb = generate_quiz_template_workbook()
    wb["Questions"]["A2"] = "NOT_A_TYPE"
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def _import_url(db_session: Session) -> str:
    return f"/api/classes/{db_session.info['class_id']}/quizzes/import"


def _preview_url(db_session: Session) -> str:
    return f"{_import_url(db_session)}/preview"


def test_preview_accepts_valid_workbook(client: TestClient, db_session: Session) -> None:
    response = client.post(
        _preview_url(db_session),
        headers=_headers(db_session.info["trainer_id"]),
        files={"file": ("quiz.xlsx", _template_bytes(), XLSX_MIME)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert body["warnings"] == []
    assert len(body["questions"]) == 3


def test_preview_returns_valid_questions_alongside_errors(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(
        _preview_url(db_session),
        headers=_headers(db_session.info["trainer_id"]),
        files={"file": ("quiz.xlsx", _invalid_template_bytes(), XLSX_MIME)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert len(body["errors"]) == 1
    assert len(body["questions"]) == 2


def test_import_persists_quiz_with_questions_and_options(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(
        _import_url(db_session),
        headers=_headers(db_session.info["trainer_id"]),
        data={"title": "  Imported Quiz  ", "description": " From Excel "},
        files={"file": ("quiz.xlsx", _template_bytes(), XLSX_MIME)},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Imported Quiz"
    assert body["description"] == "From Excel"
    assert body["status"] == "DRAFT"
    assert len(body["questions"]) == 3

    fill_in = next(q for q in body["questions"] if q["question_type"] == "FILL_IN_BLANK")
    assert fill_in["grading_config"][FILL_IN_BLANK_ANSWERS_KEY] == [
        "HTTPS",
        "HTTP over TLS",
    ]

    db_session.expire_all()
    assert db_session.query(Quiz).count() == 1


def test_import_blocks_invalid_workbook(client: TestClient, db_session: Session) -> None:
    response = client.post(
        _import_url(db_session),
        headers=_headers(db_session.info["trainer_id"]),
        data={"title": "Broken Quiz"},
        files={"file": ("quiz.xlsx", _invalid_template_bytes(), XLSX_MIME)},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["valid"] is False
    assert detail["errors"]

    db_session.expire_all()
    assert db_session.query(Quiz).count() == 0


def test_import_rejects_non_xlsx_extension(client: TestClient, db_session: Session) -> None:
    response = client.post(
        _import_url(db_session),
        headers=_headers(db_session.info["trainer_id"]),
        data={"title": "CSV Quiz"},
        files={"file": ("quiz.csv", b"question,answer\n", "text/csv")},
    )

    assert response.status_code == 415


def test_import_rejects_xlsx_that_is_not_a_workbook(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(
        _import_url(db_session),
        headers=_headers(db_session.info["trainer_id"]),
        data={"title": "Fake Quiz"},
        files={"file": ("quiz.xlsx", b"not a zip payload", XLSX_MIME)},
    )

    assert response.status_code == 415


def test_import_requires_class_teacher(client: TestClient, db_session: Session) -> None:
    student_response = client.post(
        _import_url(db_session),
        headers=_headers(db_session.info["student_id"], "student"),
        data={"title": "Student Quiz"},
        files={"file": ("quiz.xlsx", _template_bytes(), XLSX_MIME)},
    )
    assert student_response.status_code == 403

    other_trainer_response = client.post(
        _import_url(db_session),
        headers=_headers(db_session.info["other_trainer_id"]),
        data={"title": "Other Trainer Quiz"},
        files={"file": ("quiz.xlsx", _template_bytes(), XLSX_MIME)},
    )
    assert other_trainer_response.status_code == 403


def test_import_requires_existing_class(client: TestClient, db_session: Session) -> None:
    response = client.post(
        f"/api/classes/{uuid.uuid4()}/quizzes/import",
        headers=_headers(db_session.info["trainer_id"]),
        data={"title": "Missing Class Quiz"},
        files={"file": ("quiz.xlsx", _template_bytes(), XLSX_MIME)},
    )

    assert response.status_code == 404


def test_template_download_returns_xlsx(client: TestClient, db_session: Session) -> None:
    response = client.get(
        f"{_import_url(db_session)}/template",
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MIME
    assert response.content.startswith(b"PK")

    wb = openpyxl.load_workbook(io.BytesIO(response.content))
    assert "Questions" in wb.sheetnames


def _quizzes_url(db_session: Session) -> str:
    return f"/api/classes/{db_session.info['class_id']}/quizzes"


def _import_quiz(client: TestClient, db_session: Session) -> dict:
    response = client.post(
        _import_url(db_session),
        headers=_headers(db_session.info["trainer_id"]),
        data={"title": "Review Quiz", "description": "Chapter 1"},
        files={"file": ("quiz.xlsx", _template_bytes(), XLSX_MIME)},
    )
    assert response.status_code == 201
    return response.json()


def test_list_returns_quiz_summaries(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)

    response = client.get(
        _quizzes_url(db_session),
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == imported["id"]
    assert body[0]["title"] == "Review Quiz"
    assert body[0]["description"] == "Chapter 1"
    assert body[0]["status"] == "DRAFT"
    assert body[0]["question_count"] == 3
    assert "questions" not in body[0]


def test_get_quiz_returns_questions_and_answers(client: TestClient, db_session: Session) -> None:
    imported = _import_quiz(client, db_session)

    response = client.get(
        f"{_quizzes_url(db_session)}/{imported['id']}",
        headers=_headers(db_session.info["trainer_id"]),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Review Quiz"
    assert len(body["questions"]) == 3

    fill_in = next(q for q in body["questions"] if q["question_type"] == "FILL_IN_BLANK")
    assert fill_in["grading_config"][FILL_IN_BLANK_ANSWERS_KEY] == [
        "HTTPS",
        "HTTP over TLS",
    ]
    single = next(q for q in body["questions"] if q["question_type"] == "SINGLE_CHOICE")
    assert any(option["is_correct"] for option in single["options"])


def test_quiz_list_requires_class_teacher(client: TestClient, db_session: Session) -> None:
    _import_quiz(client, db_session)

    student_response = client.get(
        _quizzes_url(db_session),
        headers=_headers(db_session.info["student_id"], "student"),
    )
    assert student_response.status_code == 403

    missing_response = client.get(
        f"{_quizzes_url(db_session)}/{uuid.uuid4()}",
        headers=_headers(db_session.info["trainer_id"]),
    )
    assert missing_response.status_code == 404
