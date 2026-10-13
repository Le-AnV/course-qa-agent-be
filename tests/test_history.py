"""Tests for Chat History endpoints and service (AI-95, Epic E7).

Covers AC-01 through AC-07:
- AC-01, AC-02, AC-03: save_chat_turn and message persistence
- AC-04, AC-06: GET /api/history list sessions, sorted updated_at DESC
- AC-05, AC-07: GET /api/history/{session_id} transcript, ordered created_at ASC
- Row-level security: 403 Forbidden when accessing another user's session
- Cascade deletion: DELETE /api/history/{session_id}
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.database as dbmod
from app import models  # noqa: F401
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.seed import seed_database
from app.main import app
from app.models.course import Class, Course
from app.models.user import User
from app.services import history_service

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch) -> Generator[Session, None, None]:
    """Provides an isolated in-memory SQLite session with all tables created."""
    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)
    monkeypatch.setattr(settings, "email_dry_run", True)

    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dbmod._db_url = TEST_DB_URL
    Base.metadata.create_all(bind=engine)

    SessionLocal = get_session_local(TEST_DB_URL)
    db: Session = SessionLocal()
    try:
        seed_database(db)
        # Create a sample course and class for testing
        course = Course(code="CS101", name="Intro to Computer Science")
        db.add(course)
        db.flush()

        trainer = db.query(User).filter(User.email == "teacher@academic.edu.vn").first()
        test_class = Class(
            course_id=course.id,
            trainer_id=trainer.id,
            code="CS101-A",
            name="Class A",
            semester="1",
            academic_year="2026-2027",
        )
        db.add(test_class)
        db.commit()

        yield db
    finally:
        db.close()
        engine.dispose()
        reset_engine()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient with overridden get_db dependency."""
    def _override() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _get_test_class_id(db_session: Session) -> uuid.UUID:
    clazz = db_session.query(Class).first()
    assert clazz is not None
    return clazz.id


def test_list_history_unauthenticated_returns_401(client: TestClient) -> None:
    """AC-04: Unauthenticated request must return 401 Unauthorized."""
    res = client.get("/api/history")
    assert res.status_code == 401


def test_list_history_initially_empty(client: TestClient, db_session: Session) -> None:
    """AC-04: A new student initially has an empty session list."""
    headers = _login(client, "student@academic.edu.vn", "Student@123")
    res = client.get("/api/history", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_create_and_list_sessions(client: TestClient, db_session: Session) -> None:
    """AC-03, AC-04, AC-06: Create a session and verify it is returned sorted newest first."""
    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))

    # 1. Create first session
    create_res1 = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "First Session"},
    )
    assert create_res1.status_code == 201
    s1_id = create_res1.json()["id"]

    # 2. Create second session
    create_res2 = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Second Session"},
    )
    assert create_res2.status_code == 201
    s2_id = create_res2.json()["id"]

    # 3. List sessions: s2 must appear before s1 (updated_at DESC)
    list_res = client.get(f"/api/history?class_id={class_id}", headers=headers)
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    assert len(items) == 2
    assert items[0]["id"] == s2_id
    assert items[1]["id"] == s1_id


def test_append_message_and_retrieve_detail(client: TestClient, db_session: Session) -> None:
    """AC-01, AC-02, AC-05, AC-07: Append messages and load chronological transcript."""
    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))

    # Create session
    create_res = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Algorithm Query"},
    )
    session_id = create_res.json()["id"]

    # Append user question
    msg1 = client.post(
        f"/api/history/sessions/{session_id}/messages",
        headers=headers,
        json={"role": "USER", "content": "What is Dijkstra algorithm?", "used_web_search": False},
    )
    assert msg1.status_code == 201

    # Append assistant answer
    msg2 = client.post(
        f"/api/history/sessions/{session_id}/messages",
        headers=headers,
        json={
            "role": "ASSISTANT",
            "content": "Dijkstra is a shortest path algorithm.",
            "used_web_search": True,
        },
    )
    assert msg2.status_code == 201
    assert msg2.json()["used_web_search"] is True

    # Retrieve transcript
    detail_res = client.get(f"/api/history/{session_id}", headers=headers)
    assert detail_res.status_code == 200
    messages = detail_res.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "USER"
    assert messages[0]["content"] == "What is Dijkstra algorithm?"
    assert messages[1]["role"] == "ASSISTANT"
    assert messages[1]["used_web_search"] is True


def test_row_level_security_forbidden(client: TestClient, db_session: Session) -> None:
    """User B cannot view or append messages to User A's session (403 Forbidden)."""
    student_headers = _login(client, "student@academic.edu.vn", "Student@123")
    trainer_headers = _login(client, "teacher@academic.edu.vn", "Teacher@123")
    class_id = str(_get_test_class_id(db_session))

    # Student creates session
    create_res = client.post(
        "/api/history/sessions",
        headers=student_headers,
        json={"class_id": class_id, "title": "Private Discussion"},
    )
    session_id = create_res.json()["id"]

    # Trainer tries to access student's session -> 403 Forbidden
    res = client.get(f"/api/history/{session_id}", headers=trainer_headers)
    assert res.status_code == 403
    assert "not own this chat session" in res.json()["detail"]


def test_non_existent_session_returns_404(client: TestClient) -> None:
    """Querying a non-existent session UUID returns 404 Not Found."""
    headers = _login(client, "student@academic.edu.vn", "Student@123")
    fake_id = str(uuid.uuid4())
    res = client.get(f"/api/history/{fake_id}", headers=headers)
    assert res.status_code == 404


def test_delete_session_cascades(client: TestClient, db_session: Session) -> None:
    """AC-05: Deleting a session cascades deletion and is no longer retrievable."""
    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))

    create_res = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "To be deleted"},
    )
    session_id = create_res.json()["id"]

    # Add message
    client.post(
        f"/api/history/sessions/{session_id}/messages",
        headers=headers,
        json={"role": "USER", "content": "Temporary message"},
    )

    # Delete session
    del_res = client.delete(f"/api/history/{session_id}", headers=headers)
    assert del_res.status_code == 200

    # Verify session is gone
    get_res = client.get(f"/api/history/{session_id}", headers=headers)
    assert get_res.status_code == 404


def test_save_chat_turn_service_auto_creates_session_and_truncates_title(
    db_session: Session,
) -> None:
    """AC-01, AC-02, AC-03: save_chat_turn initializes session with first 50 chars as title."""
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    clazz = db_session.query(Class).first()

    long_prompt = (
        "Explain to me the fundamentals of database normalization including 1NF 2NF 3NF and BCNF"
    )
    session, user_msg, assistant_msg = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=clazz.id,
        user_content=long_prompt,
        assistant_content="Normalization reduces redundancy...",
        session_id=None,
        used_web_search=False,
    )

    assert session is not None
    assert len(session.title) <= 50
    assert user_msg.role == "USER"
    assert assistant_msg.role == "ASSISTANT"
    assert assistant_msg.used_web_search is False
