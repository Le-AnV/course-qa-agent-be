"""HTTP contract and class authorization tests for AI-61."""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register all tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.course import Class, Enrollment
from app.models.document import Document
from app.models.user import Role, User
from app.routers.chat import router as chat_router

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch, tmp_path) -> Generator[Session, None, None]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)
    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dbmod._db_url = TEST_DB_URL
    Base.metadata.create_all(bind=engine)

    db = get_session_local(TEST_DB_URL)()
    try:
        student_role = Role(name="STUDENT")
        trainer_role = Role(name="TRAINER")
        db.add_all([student_role, trainer_role])
        db.flush()

        trainer = User(
            full_name="Trainer",
            email="trainer-ai61@example.com",
            role_id=trainer_role.id,
            status="ACTIVE",
        )
        student = User(
            full_name="Student",
            email="student-ai61@example.com",
            role_id=student_role.id,
            status="ACTIVE",
        )
        db.add_all([trainer, student])
        db.flush()

        class_a = Class(
            trainer_id=trainer.id,
            code="AI61-A",
            name="AI-61 Class A",
            semester="HK1",
            academic_year="2026-2027",
            status="ACTIVE",
        )
        class_b = Class(
            trainer_id=trainer.id,
            code="AI61-B",
            name="AI-61 Class B",
            semester="HK1",
            academic_year="2026-2027",
            status="ACTIVE",
        )
        db.add_all([class_a, class_b])
        db.flush()
        db.add(Enrollment(class_id=class_a.id, student_id=student.id, status="ACTIVE"))
        db.commit()

        db.info["student_id"] = student.id
        db.info["trainer_id"] = trainer.id
        db.info["class_a_id"] = class_a.id
        db.info["class_b_id"] = class_b.id
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


@pytest.fixture()
def student_headers(db_session: Session) -> dict[str, str]:
    token = create_access_token(str(db_session.info["student_id"]), "student")
    return {"Authorization": f"Bearer {token}"}


def _event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def test_chat_requires_authentication(client: TestClient, db_session: Session) -> None:
    response = client.post(
        "/api/chat",
        json={"message": "What is unit 1?", "class_id": str(db_session.info["class_a_id"])},
    )

    assert response.status_code == 401


def test_chat_rejects_class_outside_student_enrollment(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
) -> None:
    response = client.post(
        "/api/chat",
        headers=student_headers,
        json={"message": "What is unit 1?", "class_id": str(db_session.info["class_b_id"])},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "You are not a member of this class."


def test_chat_streams_sse_for_an_enrolled_student(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_stream_chat(*, db, message, class_id, **kwargs):
        captured.update(db=db, message=message, class_id=class_id, **kwargs)
        yield _event("token", {"text": "Neural networks"})
        yield _event(
            "citation",
            {
                "index": 1,
                "kind": "curriculum",
                "title": "lesson.pdf",
                "document_id": "doc-1",
                "page": 2,
                "snippet": "A neural network is...",
                "url": None,
            },
        )
        yield _event(
            "done",
            {
                "source": "curriculum",
                "used_web_search": False,
                "top_score": 0.91,
            },
        )

    monkeypatch.setattr(chat_router.chat_service, "stream_chat", fake_stream_chat)

    class_id = db_session.info["class_a_id"]
    response = client.post(
        "/api/chat",
        headers=student_headers,
        json={"message": "  Explain neural networks.  ", "class_id": str(class_id)},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: token" in response.text
    assert "event: citation" in response.text
    assert "event: done" in response.text
    assert captured == {
        "db": db_session,
        "message": "Explain neural networks.",
        "class_id": class_id,
        "user_id": db_session.info["student_id"],
        "session_id": None,
        "history": [],
    }

    from app.services import quota_service

    student = db_session.get(User, db_session.info["student_id"])
    assert student is not None
    assert quota_service.get_status(db_session, student).used == 1


def test_chat_loads_context_for_an_owned_session(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
    monkeypatch,
) -> None:
    from app.models.chat import ChatMessage, ChatSession

    session = ChatSession(
        user_id=db_session.info["student_id"],
        class_id=db_session.info["class_a_id"],
        title="Policy questions",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    created = datetime.now(UTC)
    db_session.add_all(
        [
            ChatMessage(
                session_id=session.id,
                role="USER",
                content="What is the policy?",
                created_at=created,
            ),
            ChatMessage(
                session_id=session.id,
                role="ASSISTANT",
                content="The policy is...",
                created_at=created + timedelta(milliseconds=1),
            ),
        ]
    )
    db_session.commit()

    captured: dict[str, object] = {}

    async def fake_stream_chat(*, db, message, class_id, user_id, session_id, history):
        captured.update(
            db=db,
            message=message,
            class_id=class_id,
            user_id=user_id,
            session_id=session_id,
            history=history,
        )
        yield _event(
            "done",
            {"source": "none", "used_web_search": False, "threshold": 0.8, "top_score": None},
        )

    monkeypatch.setattr(chat_router.chat_service, "stream_chat", fake_stream_chat)

    response = client.post(
        "/api/chat",
        headers=student_headers,
        json={
            "message": "So when is the deadline?",
            "class_id": str(db_session.info["class_a_id"]),
            "session_id": str(session.id),
        },
    )

    assert response.status_code == 200
    assert captured["session_id"] == session.id
    assert [exchange.user for exchange in captured["history"]] == ["What is the policy?"]


def test_chat_rejects_another_users_session(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    from app.models.chat import ChatSession

    trainer_headers = _trainer_headers(db_session)
    session = ChatSession(
        user_id=db_session.info["student_id"],
        class_id=db_session.info["class_a_id"],
        title="Private",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    async def fake_stream_chat(**_kwargs):
        raise AssertionError("The stream must not start for another user's session.")
        yield  # pragma: no cover

    monkeypatch.setattr(chat_router.chat_service, "stream_chat", fake_stream_chat)

    response = client.post(
        "/api/chat",
        headers=trainer_headers,
        json={
            "message": "So when is the deadline?",
            "class_id": str(db_session.info["class_a_id"]),
            "session_id": str(session.id),
        },
    )

    assert response.status_code == 403
    assert "not own this chat session" in response.json()["detail"]


def test_student_opens_a_cited_class_document(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
    tmp_path,
) -> None:
    document_id = uuid.uuid4()
    source = tmp_path / f"{document_id}.pdf"
    source.write_bytes(b"test-pdf-content")
    db_session.add(
        Document(
            id=document_id,
            class_id=db_session.info["class_a_id"],
            uploaded_by=db_session.info["trainer_id"],
            file_name="lesson.pdf",
            file_type=".pdf",
            mime_type="application/pdf",
            size_bytes=16,
            storage_path=str(source),
            status="indexed",
        )
    )
    db_session.commit()

    response = client.get(f"/api/chat/sources/{document_id}", headers=student_headers)

    assert response.status_code == 200
    assert response.content == b"test-pdf-content"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")


def _archive_class(db_session: Session, class_id: uuid.UUID) -> None:
    clazz = db_session.get(Class, class_id)
    assert clazz is not None
    clazz.status = "ARCHIVED"
    db_session.commit()


def _trainer_headers(db_session: Session) -> dict[str, str]:
    token = create_access_token(str(db_session.info["trainer_id"]), "trainer")
    return {"Authorization": f"Bearer {token}"}


def test_chat_rejects_an_archived_class_for_an_enrolled_student(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
    monkeypatch,
) -> None:
    """AI-127: archived classes are hidden from students, including chat."""

    class_id = db_session.info["class_b_id"]
    db_session.add(
        Enrollment(
            class_id=class_id,
            student_id=db_session.info["student_id"],
            status="ACTIVE",
        )
    )
    _archive_class(db_session, class_id)

    async def fake_stream_chat(*, db, message, class_id, **kwargs):
        yield _event(
            "done",
            {
                "source": "curriculum",
                "used_web_search": False,
                "top_score": 0.91,
            },
        )

    monkeypatch.setattr(chat_router.chat_service, "stream_chat", fake_stream_chat)

    response = client.post(
        "/api/chat",
        headers=student_headers,
        json={"message": "What is unit 1?", "class_id": str(class_id)},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Class not found."


def test_chat_rejects_an_archived_class_for_its_trainer(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    """AI-127: archived classes leave the trainer workspace too."""

    class_id = db_session.info["class_b_id"]
    _archive_class(db_session, class_id)

    async def fake_stream_chat(*, db, message, class_id, **kwargs):
        yield _event(
            "done",
            {
                "source": "curriculum",
                "used_web_search": False,
                "top_score": 0.91,
            },
        )

    monkeypatch.setattr(chat_router.chat_service, "stream_chat", fake_stream_chat)

    response = client.post(
        "/api/chat",
        headers=_trainer_headers(db_session),
        json={"message": "What is unit 1?", "class_id": str(class_id)},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Class not found."


def test_admin_can_still_chat_in_an_archived_class(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    """AI-127: admins keep access to archived classes so they can restore them."""

    admin_role = db_session.query(Role).filter(Role.name == "ADMIN").first()
    assert admin_role is not None
    admin = db_session.query(User).filter(User.email == "admin-ai61@example.com").first()
    if admin is None:
        admin = User(
            full_name="Admin",
            email="admin-ai61@example.com",
            role_id=admin_role.id,
            status="ACTIVE",
        )
        db_session.add(admin)
        db_session.commit()

    class_id = db_session.info["class_b_id"]
    _archive_class(db_session, class_id)

    async def fake_stream_chat(*, db, message, class_id, **kwargs):
        yield _event(
            "done",
            {
                "source": "curriculum",
                "used_web_search": False,
                "top_score": 0.91,
            },
        )

    monkeypatch.setattr(chat_router.chat_service, "stream_chat", fake_stream_chat)
    token = create_access_token(str(admin.id), "admin")

    response = client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "What is unit 1?", "class_id": str(class_id)},
    )

    assert response.status_code == 200


def test_student_cannot_open_a_source_from_an_archived_class(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
    tmp_path,
) -> None:
    """The source viewer must enforce the same archived-class boundary as chat."""

    class_id = db_session.info["class_b_id"]
    db_session.add(
        Enrollment(
            class_id=class_id,
            student_id=db_session.info["student_id"],
            status="ACTIVE",
        )
    )
    _archive_class(db_session, class_id)

    document_id = uuid.uuid4()
    source = tmp_path / f"{document_id}.pdf"
    source.write_bytes(b"archived-pdf-content")
    db_session.add(
        Document(
            id=document_id,
            class_id=class_id,
            uploaded_by=db_session.info["trainer_id"],
            file_name="archived.pdf",
            file_type=".pdf",
            mime_type="application/pdf",
            size_bytes=21,
            storage_path=str(source),
            status="indexed",
        )
    )
    db_session.commit()

    response = client.get(f"/api/chat/sources/{document_id}", headers=student_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Class not found."


def test_chat_quota_reports_and_enforces_the_daily_limit(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
) -> None:
    from app.models.usage import ChatDailyUsage
    from app.services import quota_service

    db_session.add(
        ChatDailyUsage(
            user_id=db_session.info["student_id"],
            usage_date=quota_service._today(),
            used=settings.student_daily_question_limit,
        )
    )
    db_session.commit()

    quota = client.get("/api/chat/quota", headers=student_headers)

    assert quota.status_code == 200
    assert quota.json()["remaining"] == 0
    assert quota.json()["used"] == settings.student_daily_question_limit

    response = client.post(
        "/api/chat",
        headers=student_headers,
        json={"message": "What is unit 1?", "class_id": str(db_session.info["class_a_id"])},
    )

    assert response.status_code == 429
    assert "Daily question limit reached" in response.json()["detail"]


def test_failed_answer_refunds_the_question(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
    monkeypatch,
) -> None:
    from app.services import quota_service

    async def fake_stream_chat(**_kwargs):
        yield _event("error", {"message": "boom"})

    monkeypatch.setattr(chat_router.chat_service, "stream_chat", fake_stream_chat)
    student = db_session.get(User, db_session.info["student_id"])
    assert student is not None
    before = quota_service.get_status(db_session, student)

    response = client.post(
        "/api/chat",
        headers=student_headers,
        json={"message": "What is unit 1?", "class_id": str(db_session.info["class_a_id"])},
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    after = quota_service.get_status(db_session, student)
    assert after.used == before.used


def test_stream_token_containing_done_still_refunds_on_error(
    client: TestClient,
    student_headers: dict[str, str],
    db_session: Session,
    monkeypatch,
) -> None:
    from app.services import quota_service

    async def fake_stream_chat(**_kwargs):
        yield _event("token", {"text": "event: done"})
        yield _event("error", {"message": "boom"})

    monkeypatch.setattr(chat_router.chat_service, "stream_chat", fake_stream_chat)
    student = db_session.get(User, db_session.info["student_id"])
    assert student is not None
    before = quota_service.get_status(db_session, student)

    response = client.post(
        "/api/chat",
        headers=student_headers,
        json={"message": "What is unit 1?", "class_id": str(db_session.info["class_a_id"])},
    )

    assert response.status_code == 200
    assert "event: token" in response.text
    assert "event: error" in response.text
    after = quota_service.get_status(db_session, student)
    assert after.used == before.used

