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
from datetime import UTC, datetime, timedelta

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
from app.models.chat import ChatMessage, ChatSession
from app.models.course import Class
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
        # Standalone test class (no course binding).
        trainer = db.query(User).filter(User.email == "teacher@academic.edu.vn").first()
        test_class = Class(
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

    # In fast in-memory SQLite, both sessions can be created in the same second.
    # Set an earlier updated_at on s1 to guarantee deterministic ordering:
    s1_obj = db_session.get(ChatSession, uuid.UUID(s1_id))
    assert s1_obj is not None
    s1_obj.updated_at = datetime.now(UTC) - timedelta(minutes=5)
    s1_obj.created_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.commit()

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


def test_delete_session_does_not_affect_other_sessions(
    client: TestClient, db_session: Session
) -> None:
    """AC-01: Deleting one session leaves other sessions and their messages intact."""
    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))

    create_1 = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Session to delete"},
    )
    assert create_1.status_code == 201
    session_1_id = create_1.json()["id"]

    create_2 = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Session to keep"},
    )
    assert create_2.status_code == 201
    session_2_id = create_2.json()["id"]

    for session_id, label in ((session_1_id, "Session 1"), (session_2_id, "Session 2")):
        for role in ("USER", "ASSISTANT"):
            msg_res = client.post(
                f"/api/history/sessions/{session_id}/messages",
                headers=headers,
                json={"role": role, "content": f"{label} {role.lower()} message"},
            )
            assert msg_res.status_code == 201

    delete_res = client.delete(f"/api/history/{session_1_id}", headers=headers)
    assert delete_res.status_code == 200

    deleted_detail = client.get(f"/api/history/{session_1_id}", headers=headers)
    assert deleted_detail.status_code == 404

    kept_detail = client.get(f"/api/history/{session_2_id}", headers=headers)
    assert kept_detail.status_code == 200
    kept_messages = kept_detail.json()["messages"]
    assert len(kept_messages) == 2
    assert {message["content"] for message in kept_messages} == {
        "Session 2 user message",
        "Session 2 assistant message",
    }

    list_res = client.get(f"/api/history?class_id={class_id}", headers=headers)
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == session_2_id


def test_delete_session_rejects_another_user(client: TestClient, db_session: Session) -> None:
    """AC-02: A non-owner receives 403 and the session remains intact."""
    student_headers = _login(client, "student@academic.edu.vn", "Student@123")
    trainer_headers = _login(client, "teacher@academic.edu.vn", "Teacher@123")
    class_id = str(_get_test_class_id(db_session))

    create_res = client.post(
        "/api/history/sessions",
        headers=student_headers,
        json={"class_id": class_id, "title": "Owner only"},
    )
    assert create_res.status_code == 201
    session_id = create_res.json()["id"]

    msg_res = client.post(
        f"/api/history/sessions/{session_id}/messages",
        headers=student_headers,
        json={"role": "USER", "content": "Private question"},
    )
    assert msg_res.status_code == 201

    forbidden = client.delete(f"/api/history/{session_id}", headers=trainer_headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "Access forbidden: you do not own this chat session."

    detail_res = client.get(f"/api/history/{session_id}", headers=student_headers)
    assert detail_res.status_code == 200
    assert len(detail_res.json()["messages"]) == 1

    db_session.expire_all()
    assert db_session.get(ChatSession, uuid.UUID(session_id)) is not None


def test_delete_nonexistent_session_returns_404(client: TestClient) -> None:
    """AC-03: Deleting a random UUID returns 404 Not Found."""
    headers = _login(client, "student@academic.edu.vn", "Student@123")
    fake_id = uuid.uuid4()

    res = client.delete(f"/api/history/{fake_id}", headers=headers)
    assert res.status_code == 404


def test_delete_session_purges_content_gap_vectors(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03: Web-search vectors are purged and all messages cascade-delete."""
    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))

    create_res = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Qdrant cleanup"},
    )
    assert create_res.status_code == 201
    session_id = create_res.json()["id"]
    session_uuid = uuid.UUID(session_id)

    normal_msg = client.post(
        f"/api/history/sessions/{session_id}/messages",
        headers=headers,
        json={"role": "USER", "content": "Normal question", "used_web_search": False},
    )
    assert normal_msg.status_code == 201

    web_msg = client.post(
        f"/api/history/sessions/{session_id}/messages",
        headers=headers,
        json={
            "role": "ASSISTANT",
            "content": "Answer with web fallback",
            "used_web_search": True,
        },
    )
    assert web_msg.status_code == 201
    assert web_msg.json()["used_web_search"] is True
    web_msg_id = web_msg.json()["id"]

    assert db_session.query(ChatMessage).filter(ChatMessage.session_id == session_uuid).count() == 2

    captured_message_ids: list[str] = []

    def fake_delete_content_gap_queries(
        message_ids: list[str], qdrant_client: object | None = None
    ) -> None:
        captured_message_ids.extend(message_ids)

    monkeypatch.setattr(
        history_service,
        "delete_content_gap_queries",
        fake_delete_content_gap_queries,
    )

    delete_res = client.delete(f"/api/history/{session_id}", headers=headers)
    assert delete_res.status_code == 200
    assert captured_message_ids == [web_msg_id]

    db_session.expire_all()
    assert db_session.get(ChatSession, session_uuid) is None
    remaining_messages = (
        db_session.query(ChatMessage).filter(ChatMessage.session_id == session_uuid).count()
    )
    assert remaining_messages == 0


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


def test_message_citations_survive_history_reload(client: TestClient, db_session: Session) -> None:
    """AI-63 AC-03: reopening an old answer must keep its original citations."""

    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))
    create_res = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Cited answer"},
    )
    session_id = create_res.json()["id"]

    citation = {
        "index": 1,
        "kind": "curriculum",
        "title": "syllabus.pdf",
        "url": None,
        "snippet": "The deadline is Friday.",
        "document_id": "doc-1",
        "page": 2,
    }
    append_res = client.post(
        f"/api/history/sessions/{session_id}/messages",
        headers=headers,
        json={
            "role": "ASSISTANT",
            "content": "The deadline is Friday [1].",
            "used_web_search": False,
            "citations": [citation],
        },
    )
    assert append_res.status_code == 201

    detail_res = client.get(f"/api/history/{session_id}", headers=headers)
    assert detail_res.status_code == 200
    assert detail_res.json()["messages"][0]["citations"] == [citation]


def test_message_suggested_questions_survive_history_reload(
    client: TestClient, db_session: Session
) -> None:
    """AI-125: reopening an old answer must keep its suggested follow-up questions."""

    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))
    create_res = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Suggested questions answer"},
    )
    session_id = create_res.json()["id"]

    suggestions = [
        "What is gradient descent?",
        "How to prevent overfitting?",
    ]
    append_res = client.post(
        f"/api/history/sessions/{session_id}/messages",
        headers=headers,
        json={
            "role": "ASSISTANT",
            "content": "Deep learning models are trained via gradient descent.",
            "used_web_search": False,
            "citations": [],
            "suggested_questions": suggestions,
        },
    )
    assert append_res.status_code == 201

    detail_res = client.get(f"/api/history/{session_id}", headers=headers)
    assert detail_res.status_code == 200
    assert detail_res.json()["messages"][0]["suggested_questions"] == suggestions


def test_save_chat_turn_persists_suggested_questions(db_session: Session) -> None:
    """AI-125: save_chat_turn saves suggested_questions into assistant message."""
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    clazz = db_session.query(Class).first()

    suggestions = ["How does backprop work?", "Why use ReLU?"]
    _session, _user_msg, assistant_msg = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=clazz.id,
        user_content="What is neural network?",
        assistant_content="Neural networks are interconnected layers.",
        session_id=None,
        used_web_search=False,
        suggested_questions=suggestions,
    )

    assert assistant_msg.suggested_questions == suggestions


def test_rename_session_updates_title_without_bumping_activity(
    client: TestClient, db_session: Session
) -> None:
    """An owned session can be renamed; the sidebar activity order is preserved."""

    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))
    create_res = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Original title"},
    )
    session_id = create_res.json()["id"]
    before = db_session.get(ChatSession, uuid.UUID(session_id)).updated_at

    rename_res = client.patch(
        f"/api/history/{session_id}",
        headers=headers,
        json={"title": "  My renamed conversation  "},
    )

    assert rename_res.status_code == 200
    assert rename_res.json()["title"] == "My renamed conversation"
    db_session.expire_all()
    refreshed = db_session.get(ChatSession, uuid.UUID(session_id))
    assert refreshed.title == "My renamed conversation"
    assert refreshed.updated_at == before


def test_rename_session_rejects_another_users_session(
    client: TestClient, db_session: Session
) -> None:
    """Row-level security also applies to rename requests."""

    student_headers = _login(client, "student@academic.edu.vn", "Student@123")
    trainer_headers = _login(client, "teacher@academic.edu.vn", "Teacher@123")
    class_id = str(_get_test_class_id(db_session))
    create_res = client.post(
        "/api/history/sessions",
        headers=student_headers,
        json={"class_id": class_id, "title": "Private session"},
    )
    session_id = create_res.json()["id"]

    res = client.patch(
        f"/api/history/{session_id}",
        headers=trainer_headers,
        json={"title": "Hijacked"},
    )

    assert res.status_code == 403


def test_rename_session_rejects_blank_title(client: TestClient, db_session: Session) -> None:
    """A rename must contain at least one non-whitespace character."""

    headers = _login(client, "student@academic.edu.vn", "Student@123")
    class_id = str(_get_test_class_id(db_session))
    create_res = client.post(
        "/api/history/sessions",
        headers=headers,
        json={"class_id": class_id, "title": "Keep me"},
    )
    session_id = create_res.json()["id"]

    res = client.patch(
        f"/api/history/{session_id}",
        headers=headers,
        json={"title": "   "},
    )

    assert res.status_code == 422


# ---------------------------------------------------------------------------
# AI-97 — Reopen an Old Conversation & Continue (AC-01, AC-02, AC-03)
# ---------------------------------------------------------------------------


def test_save_chat_turn_creates_new_session_when_session_id_none(
    db_session: Session,
) -> None:
    """AC-03: When session_id is None, save_chat_turn creates a new session and 2 messages."""
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    assert student is not None
    class_id = _get_test_class_id(db_session)

    session, user_msg, assistant_msg = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=class_id,
        user_content="What are the prerequisite units?",
        assistant_content="The prerequisites are Unit 1 and Unit 2.",
        session_id=None,
    )

    assert session is not None
    assert session.user_id == student.id
    assert session.class_id == class_id
    assert session.title.startswith("What are the prerequisite")
    assert user_msg.role == "USER"
    assert user_msg.content == "What are the prerequisite units?"
    assert assistant_msg.role == "ASSISTANT"
    assert assistant_msg.content == "The prerequisites are Unit 1 and Unit 2."

    # Verify messages in DB
    detail = history_service.get_session_messages(db_session, session.id, student.id)
    assert len(detail.messages) == 2
    assert detail.messages[0].id == user_msg.id
    assert detail.messages[1].id == assistant_msg.id


def test_save_chat_turn_continues_existing_session_without_creating_new(
    db_session: Session,
) -> None:
    """AC-01, AC-02: Calling save_chat_turn twice with the same session_id appends messages to 1 session."""
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    assert student is not None
    class_id = _get_test_class_id(db_session)

    # First turn (create new session)
    session, _, _ = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=class_id,
        user_content="First question",
        assistant_content="First answer",
        session_id=None,
    )
    session_id = session.id

    # Second turn (continue existing session)
    continued_session, _, _ = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=class_id,
        user_content="Second question follow-up",
        assistant_content="Second answer with context",
        session_id=session_id,
    )

    assert continued_session.id == session_id

    # Verify exactly 4 messages in that single session in chronological order
    detail = history_service.get_session_messages(db_session, session_id, student.id)
    assert len(detail.messages) == 4
    assert detail.messages[0].content == "First question"
    assert detail.messages[1].content == "First answer"
    assert detail.messages[2].content == "Second question follow-up"
    assert detail.messages[3].content == "Second answer with context"


def test_save_chat_turn_rejects_another_users_session(
    db_session: Session,
) -> None:
    """AC-03 / Security: Reopening and continuing another user's session raises 403 Forbidden."""
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    trainer = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    assert student is not None and trainer is not None
    class_id = _get_test_class_id(db_session)

    # Student creates session
    student_session, _, _ = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=class_id,
        user_content="Student private question",
        assistant_content="Student answer",
        session_id=None,
    )

    # Trainer attempts to continue student's session -> must raise 403
    with pytest.raises(Exception) as exc_info:
        history_service.save_chat_turn(
            db=db_session,
            user_id=trainer.id,
            class_id=class_id,
            user_content="Trainer hijacking",
            assistant_content="Trainer answer",
            session_id=student_session.id,
        )
    assert "403" in str(exc_info.value) or "forbidden" in str(exc_info.value).lower()


def test_reopen_session_loads_recent_exchanges_in_order(
    db_session: Session,
) -> None:
    """AC-01: Reopening an old session loads its conversation exchanges chronologically."""
    from app.services import chat_context_service

    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    assert student is not None
    class_id = _get_test_class_id(db_session)

    session, _, _ = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=class_id,
        user_content="Explain Dijkstra algorithm",
        assistant_content="Dijkstra finds the shortest path.",
        session_id=None,
    )

    history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=class_id,
        user_content="What is its time complexity?",
        assistant_content="With binary heap, it is O((V+E) log V).",
        session_id=session.id,
    )

    exchanges = chat_context_service.load_recent_exchanges(
        db=db_session,
        session_id=session.id,
        user_id=student.id,
        class_id=class_id,
    )

    assert len(exchanges) == 2
    assert exchanges[0].user == "Explain Dijkstra algorithm"
    assert exchanges[0].assistant == "Dijkstra finds the shortest path."
    assert exchanges[1].user == "What is its time complexity?"
    assert exchanges[1].assistant == "With binary heap, it is O((V+E) log V)."


def test_new_chat_does_not_leak_context_from_old_session(
    db_session: Session,
) -> None:
    """AC-03: Switching to a new chat (session_id=None) starts fresh without old exchanges."""
    from app.services import chat_context_service

    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    assert student is not None
    class_id = _get_test_class_id(db_session)

    # Old session with exchanges
    old_session, _, _ = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=class_id,
        user_content="Old session question about python",
        assistant_content="Python is dynamically typed.",
        session_id=None,
    )

    # New session started later
    new_session, _, _ = history_service.save_chat_turn(
        db=db_session,
        user_id=student.id,
        class_id=class_id,
        user_content="Brand new session question about docker",
        assistant_content="Docker packages applications into containers.",
        session_id=None,
    )

    assert new_session.id != old_session.id

    new_exchanges = chat_context_service.load_recent_exchanges(
        db=db_session,
        session_id=new_session.id,
        user_id=student.id,
        class_id=class_id,
    )

    assert len(new_exchanges) == 1
    assert "docker" in new_exchanges[0].user.lower()
    assert "python" not in new_exchanges[0].user.lower()
