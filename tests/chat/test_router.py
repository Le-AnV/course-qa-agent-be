import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Generator

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.database as dbmod
from app import models  # noqa: F401
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_current_user, get_db
from app.core.seed import seed_database
from app.main import app
from app.models.course import Class
from app.models.user import User
from app.routers.chat import router as chat_router
from app.services import rag_service


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = "\n".join(
            line.removeprefix("data: ") for line in lines if line.startswith("data: ")
        )
        events.append((event, json.loads(data)))
    return events


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch) -> Generator[Session, None, None]:
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
    db: Session = get_session_local("sqlite://")()
    seed_database(db)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        reset_engine()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").one()

    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: student
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def unauthenticated_client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid-token"}])
def test_chat_requires_a_valid_bearer_token(
    unauthenticated_client: TestClient,
    db_session: Session,
    headers: dict[str, str],
):
    class_id = db_session.query(Class.id).first()[0]

    response = unauthenticated_client.post(
        "/api/chat",
        json={"message": "Explain recursion", "class_id": str(class_id)},
        headers=headers,
    )

    assert response.status_code == 401


def test_chat_streams_answer_as_sse_for_an_enrolled_class(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    class_id = db_session.query(Class.id).first()[0]
    document_id = uuid.uuid4()

    monkeypatch.setattr(
        rag_service,
        "retrieve_documents",
        lambda message, requested_class_id: [
            Document(
                page_content="Recursion needs a base case.",
                metadata={
                    "document_id": str(document_id),
                    "file_name": "Lecture 2.pdf",
                    "file_type": ".pdf",
                    "headers": {"Header 1": "Recursion"},
                    "page_number": 4,
                },
            )
        ],
    )

    async def stream_answer(message: str, documents) -> AsyncIterator[str]:
        yield "Answer from course materials."

    monkeypatch.setattr(rag_service, "stream_answer", stream_answer)
    response = client.post(
        "/api/chat",
        json={"message": "Explain recursion", "class_id": str(class_id)},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _parse_sse(response.text) == [
        ("token", {"text": "Answer from course materials."}),
        (
            "citation",
            {
                "marker": "[1]",
                "document_id": str(document_id),
                "file_name": "Lecture 2.pdf",
                "heading": "Recursion",
                "page": 4,
                "url": f"/api/documents/{document_id}/content?page=4",
            },
        ),
        ("done", {}),
    ]


def test_chat_rejects_a_class_the_student_is_not_enrolled_in(
    client: TestClient, db_session: Session
):
    trainer = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").one()
    other_class = Class(
        trainer_id=trainer.id,
        code="CS101-OTHER",
        name="Other class",
        semester="Fall",
        academic_year="2026",
    )
    db_session.add(other_class)
    db_session.commit()

    response = client.post(
        "/api/chat",
        json={"message": "Explain recursion", "class_id": str(other_class.id)},
    )

    assert response.status_code == 403


def test_chat_rejects_blank_question(client: TestClient, db_session: Session):
    class_id = db_session.query(Class.id).first()[0]

    response = client.post(
        "/api/chat",
        json={"message": "   ", "class_id": str(class_id)},
    )

    assert response.status_code == 422


def test_chat_returns_service_unavailable_when_retrieval_fails(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    class_id = db_session.query(Class.id).first()[0]

    def fail_retrieval(message: str, requested_class_id: str):
        raise RuntimeError("Qdrant is unavailable")

    monkeypatch.setattr(rag_service, "retrieve_documents", fail_retrieval)
    response = client.post(
        "/api/chat",
        json={"message": "Explain recursion", "class_id": str(class_id)},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Course materials are temporarily unavailable."


def test_chat_sends_error_event_when_generation_fails(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    class_id = db_session.query(Class.id).first()[0]
    monkeypatch.setattr(rag_service, "retrieve_documents", lambda message, class_id: [])

    async def fail_stream(message: str, documents) -> AsyncIterator[str]:
        raise RuntimeError("Gemini unavailable")
        yield "unreachable"

    monkeypatch.setattr(rag_service, "stream_answer", fail_stream)
    response = client.post(
        "/api/chat",
        json={"message": "Explain recursion", "class_id": str(class_id)},
    )

    assert response.status_code == 200
    assert _parse_sse(response.text) == [
        ("error", {"message": "The answer could not be generated."})
    ]
    assert "event: done" not in response.text


def test_chat_sends_a_friendly_error_when_generation_times_out(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    class_id = db_session.query(Class.id).first()[0]
    monkeypatch.setattr(settings, "chat_timeout_seconds", 0.01)
    monkeypatch.setattr(rag_service, "retrieve_documents", lambda message, class_id: [])

    async def immediate_threadpool(function, *args):
        return function(*args)

    monkeypatch.setattr(chat_router, "run_in_threadpool", immediate_threadpool)

    async def slow_stream(message: str, documents) -> AsyncIterator[str]:
        await asyncio.sleep(0.05)
        yield "late answer"

    monkeypatch.setattr(rag_service, "stream_answer", slow_stream)
    response = client.post(
        "/api/chat",
        json={"message": "Explain recursion", "class_id": str(class_id)},
    )

    assert response.status_code == 200
    assert _parse_sse(response.text) == [
        ("error", {"message": "The request timed out. Please try again."})
    ]
