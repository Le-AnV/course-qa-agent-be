from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.attendance import Holiday
from app.models.course import Class, ClassTrainer, Enrollment
from app.models.email_outbox import EmailOutbox
from app.models.system_config import SystemConfig
from app.models.ticket import Ticket, TicketReply
from app.models.user import Role, User
from app.services.ticket_service import escalate_stale_tickets


@pytest.fixture()
def db_session(monkeypatch) -> Generator[Session, None, None]:
    # Ticket tests do not exercise the external reranking model.
    monkeypatch.setattr("app.services.reranker_service.warm_up", lambda: None)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", "sqlite://")
    pg_url = os.getenv("AI135_TEST_DATABASE_URL")
    schema = "ai135_" + uuid.uuid4().hex
    if pg_url:
        from sqlalchemy import text
        admin_engine = create_engine(pg_url)
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(pg_url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
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
    student = User(
        full_name="Student",
        email="ticket-student@example.com",
        role_id=student_role.id,
        status="ACTIVE",
    )
    other_student = User(
        full_name="Other Student",
        email="ticket-other@example.com",
        role_id=student_role.id,
        status="ACTIVE",
    )
    trainer = User(
        full_name="Trainer",
        email="ticket-trainer@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    db.add_all([student, other_student, trainer])
    db.flush()
    clazz = Class(
        trainer_id=trainer.id,
        code="AI81-A",
        name="AI-81 Class A",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    other_class = Class(
        trainer_id=trainer.id,
        code="AI81-B",
        name="AI-81 Class B",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db.add_all([clazz, other_class])
    db.flush()
    db.add(Enrollment(class_id=clazz.id, student_id=student.id, status="ACTIVE"))
    db.commit()
    db.info.update(
        student_id=student.id,
        other_student_id=other_student.id,
        trainer_id=trainer.id,
        class_id=clazz.id,
        other_class_id=other_class.id,
    )
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        if pg_url:
            with admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin_engine.dispose()
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
    token = create_access_token(str(user_id), role)
    return {"Authorization": f"Bearer {token}"}


def test_answer_feedback_persists_without_creating_ticket(client, db_session):
    from app.services.history_service import save_chat_turn

    _, question, answer = save_chat_turn(
        db_session, db_session.info["student_id"], db_session.info["class_id"],
        "Original question", "Complete answer",
    )
    url = f"/api/chat/messages/{answer.id}/feedback"
    headers = _headers(db_session.info["student_id"])
    for choice in ("not_helpful", "helpful"):
        response = client.put(url, headers=headers, json={"feedback": choice})
        assert response.status_code == 200
        assert response.json()["feedback"] == choice
        db_session.refresh(answer)
        assert answer.feedback == choice
    assert answer.parent_message_id == question.id
    assert db_session.query(Ticket).count() == 0
    history = client.get(f"/api/history/{answer.session_id}", headers=headers)
    assert history.json()["messages"][-1]["feedback"] == "helpful"
    assert history.json()["messages"][-1]["ticket_id"] is None
    assert client.put(f"/api/chat/messages/{question.id}/feedback", headers=headers,
                      json={"feedback": "helpful"}).status_code == 409
    assert client.put(url, headers=headers, json={"feedback": "invalid"}).status_code == 422
    assert client.put(url, headers=_headers(db_session.info["other_student_id"]),
                      json={"feedback": "helpful"}).status_code == 404
    assert client.put(url, headers=_headers(db_session.info["trainer_id"], "trainer"),
                      json={"feedback": "helpful"}).status_code == 403


def test_confirmed_answer_ticket_snapshot_and_retry(client, db_session):
    from app.services.history_service import save_chat_turn

    citation = {"index": 1, "kind": "internet", "title": "Source", "url": "https://example.com", "snippet": "Evidence"}
    session, _, answer = save_chat_turn(
        db_session, db_session.info["student_id"], db_session.info["class_id"],
        "Original question", "A" * 12000, citations=[citation],
    )
    headers = _headers(db_session.info["student_id"])
    url = f"/api/chat/messages/{answer.id}/ticket"
    assert client.post(url, headers=headers, json={"confirmed": True}).status_code == 409
    client.put(f"/api/chat/messages/{answer.id}/feedback", headers=headers, json={"feedback": "not_helpful"})
    for invalid in ({}, {"confirmed": False}, {"confirmed": "true"}):
        assert client.post(url, headers=headers, json=invalid).status_code == 422
    assert db_session.query(Ticket).count() == 0
    created = client.post(url, headers=headers, json={"confirmed": True, "details": "Please explain"})
    assert created.status_code == 201
    ticket = created.json()
    assert ticket["original_question"] == "Original question"
    assert ticket["agent_answer"] == "A" * 12000
    assert ticket["citations"][0]["title"] == "Source"
    assert ticket["source_message_id"] == str(answer.id)
    inbox = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))
    assert inbox.status_code == 200
    assert inbox.json()["items"][0]["citations"][0]["title"] == "Source"
    assert client.post(url, headers=_headers(db_session.info["other_student_id"]), json={"confirmed": True}).status_code == 404
    assert client.post(url, headers=_headers(db_session.info["trainer_id"], "trainer"), json={"confirmed": True}).status_code == 403
    assert client.post(url, headers=headers, json={"confirmed": True, "class_id": str(db_session.info["other_class_id"])}).status_code == 422
    client.put(f"/api/chat/messages/{answer.id}/feedback", headers=headers, json={"feedback": "helpful"})
    repeated = client.post(url, headers=headers, json={"confirmed": True, "details": "Different"})
    assert repeated.status_code == 200
    assert repeated.json()["id"] == ticket["id"]
    assert db_session.query(Ticket).count() == 1
    assert db_session.query(TicketReply).count() == 1
    history = client.get(f"/api/history/{session.id}", headers=headers)
    assert history.status_code == 200
    saved_answer = history.json()["messages"][-1]
    assert saved_answer["feedback"] == "helpful"
    assert saved_answer["ticket_id"] == ticket["id"]


@pytest.mark.skipif(not os.getenv("AI135_TEST_DATABASE_URL"), reason="PostgreSQL test URL required")
def test_answer_ticket_concurrency_and_delete_snapshot(db_session):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import select

    from app.models.chat import ChatSession
    from app.services.answer_feedback_service import create_answer_ticket
    from app.services.history_service import save_chat_turn

    session, _, answer = save_chat_turn(
        db_session, db_session.info["student_id"], db_session.info["class_id"], "Question", "Answer",
    )
    answer.feedback = "not_helpful"
    db_session.commit()
    message_id, student_id, session_id = answer.id, db_session.info["student_id"], session.id

    def submit():
        with Session(db_session.bind) as independent:
            user = independent.get(User, student_id)
            ticket, _ = create_answer_ticket(independent, user, message_id, "Details")
            ticket_id = ticket.id
            independent.commit()
            return ticket_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert results[0] == results[1]
    assert db_session.query(Ticket).count() == 1
    assert db_session.query(TicketReply).count() == 1
    db_session.expire_all()
    db_session.delete(db_session.get(ChatSession, session_id))
    db_session.commit()
    db_session.expire_all()
    ticket = db_session.scalar(select(Ticket))
    assert ticket.source_message_id is None
    assert ticket.original_question == "Question"
    assert ticket.agent_answer == "Answer"
    assert ticket.citations == []


@pytest.mark.parametrize("condition", ["missing_parent", "inactive_class", "inactive_enrollment"])
def test_answer_ticket_rejects_unavailable_context(client, db_session, condition):
    from app.services.history_service import save_chat_turn

    _, _, answer = save_chat_turn(
        db_session, db_session.info["student_id"], db_session.info["class_id"], "Q", "A",
    )
    answer.feedback = "not_helpful"
    if condition == "missing_parent":
        answer.parent_message_id = None
    elif condition == "inactive_class":
        db_session.get(Class, db_session.info["class_id"]).status = "ARCHIVED"
    else:
        db_session.query(Enrollment).first().status = "INACTIVE"
    db_session.commit()
    response = client.post(f"/api/chat/messages/{answer.id}/ticket",
                           headers=_headers(db_session.info["student_id"]), json={"confirmed": True})
    assert response.status_code == (409 if condition == "missing_parent" else 403)
    assert db_session.query(Ticket).count() == 0


def test_student_can_create_open_ticket_and_add_follow_up(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(
        "/api/tickets",
        headers=_headers(db_session.info["student_id"]),
        json={
            "class_id": str(db_session.info["class_id"]),
            "question": "How does a list comprehension work?",
            "details": "I am confused about the condition part.",
        },
    )

    assert response.status_code == 201
    ticket = response.json()
    assert ticket["id"]
    assert ticket["status"] == "OPEN"
    assert ticket["student_id"] == str(db_session.info["student_id"])
    assert ticket["class_id"] == str(db_session.info["class_id"])
    assert ticket["question"] == "How does a list comprehension work?"

    reply_response = client.post(
        f"/api/tickets/{ticket['id']}/replies",
        headers=_headers(db_session.info["student_id"]),
        json={"message": "Could you show another example?"},
    )
    assert reply_response.status_code == 201
    assert reply_response.json()["message"] == "Could you show another example?"
    db_session.expire_all()
    saved = db_session.get(Ticket, uuid.UUID(ticket["id"]))
    assert saved is not None
    assert saved.status == "OPEN"
    assert [reply.message for reply in saved.replies] == [
        "I am confused about the condition part.",
        "Could you show another example?",
    ]
    assert all(reply.user_id == db_session.info["student_id"] for reply in saved.replies)


def test_trainer_answer_queues_an_email_to_the_student(client, db_session):
    """AI-85 AC-03: the student is notified when the trainer answers."""
    created = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Please explain this.",
    })
    ticket_id = created.json()["id"]
    reply = client.post(
        f"/api/tickets/{ticket_id}/replies",
        headers=_headers(db_session.info["trainer_id"], "trainer"),
        json={"message": "Here is the explanation."},
    )
    assert reply.status_code == 201

    queued = db_session.query(EmailOutbox).filter(
        EmailOutbox.to_email == "ticket-student@example.com"
    ).all()
    assert len(queued) == 1
    assert queued[0].user_id == db_session.info["student_id"]
    assert "Trainer" in queued[0].body
    assert "Here is the explanation." in queued[0].body


def test_trainer_answer_requires_student_confirmation_to_resolve(client, db_session):
    created = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Please explain this.",
    })
    ticket_id = created.json()["id"]
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")
    student_headers = _headers(db_session.info["student_id"])

    reply = client.post(f"/api/tickets/{ticket_id}/replies", headers=trainer_headers,
                        json={"message": "Here is the explanation."})
    assert reply.status_code == 201
    assert client.get(f"/api/tickets/{ticket_id}", headers=student_headers).json()["status"] == "ANSWERED"
    assert client.post(f"/api/tickets/{ticket_id}/resolve", headers=student_headers).json()["status"] == "RESOLVED"


def test_student_can_follow_up_after_a_teacher_answer(client, db_session):
    created = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Please explain this.",
    })
    ticket_id = created.json()["id"]
    student_headers = _headers(db_session.info["student_id"])
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")

    assert client.post(
        f"/api/tickets/{ticket_id}/replies", headers=trainer_headers,
        json={"message": "First answer"},
    ).status_code == 201
    assert client.get(
        f"/api/tickets/{ticket_id}", headers=student_headers
    ).json()["status"] == "ANSWERED"

    follow_up = client.post(
        f"/api/tickets/{ticket_id}/replies", headers=student_headers,
        json={"message": "Can you explain more?"},
    )
    assert follow_up.status_code == 201
    thread = client.get(f"/api/tickets/{ticket_id}", headers=student_headers).json()
    assert thread["status"] == "OPEN"
    assert [reply["message"] for reply in thread["replies"]] == [
        "First answer", "Can you explain more?"
    ]

    assert client.post(
        f"/api/tickets/{ticket_id}/replies", headers=trainer_headers,
        json={"message": "Second answer"},
    ).status_code == 201
    assert client.post(
        f"/api/tickets/{ticket_id}/resolve", headers=student_headers
    ).json()["status"] == "RESOLVED"
    assert client.post(
        f"/api/tickets/{ticket_id}/replies", headers=student_headers,
        json={"message": "Too late"},
    ).status_code == 409


def test_follow_up_on_an_old_answered_ticket_restarts_expiration(client, db_session):
    ticket = Ticket(
        class_id=db_session.info["class_id"],
        student_id=db_session.info["student_id"],
        question="Answered ten days ago?",
        status="ANSWERED",
        created_at=datetime.now(UTC) - timedelta(days=10),
    )
    db_session.add(ticket)
    db_session.commit()
    student_headers = _headers(db_session.info["student_id"])

    assert client.post(
        f"/api/tickets/{ticket.id}/replies", headers=student_headers,
        json={"message": "One more question"},
    ).status_code == 201

    listed = client.get("/api/tickets/mine?status=OPEN", headers=student_headers).json()
    assert [item["id"] for item in listed["items"]] == [str(ticket.id)]
    db_session.expire_all()
    saved = db_session.get(Ticket, ticket.id)
    assert saved is not None
    assert saved.status == "OPEN"
    assert saved.reopened_at is not None


def test_unanswered_tickets_expire_after_seven_days(client, db_session):
    now = datetime.now(UTC)
    db_session.add_all([
        Ticket(class_id=db_session.info["class_id"], student_id=db_session.info["student_id"],
               question="Six days old?", created_at=now - timedelta(days=6)),
        Ticket(class_id=db_session.info["class_id"], student_id=db_session.info["student_id"],
               question="Eight days old?", created_at=now - timedelta(days=8)),
    ])
    db_session.commit()

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))
    assert response.status_code == 200
    statuses = {item["question"]: item["status"] for item in response.json()["items"]}
    assert statuses == {"Six days old?": "OPEN", "Eight days old?": "EXPIRED"}


def test_trainer_resolves_and_reopens_an_answered_ticket(client, db_session):
    created = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Please explain this.",
    })
    ticket_id = created.json()["id"]
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")

    reply = client.post(f"/api/tickets/{ticket_id}/replies", headers=trainer_headers,
                        json={"message": "Here is the explanation."})
    assert reply.status_code == 201

    resolved = client.post(f"/api/tickets/{ticket_id}/resolve", headers=trainer_headers)
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"

    reopened = client.post(f"/api/tickets/{ticket_id}/reopen", headers=trainer_headers)
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "ANSWERED"


def test_trainer_resolves_an_open_ticket_and_reopens_it_to_open(client, db_session):
    created = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Already handled offline.",
    })
    ticket_id = created.json()["id"]
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")

    resolved = client.post(f"/api/tickets/{ticket_id}/resolve", headers=trainer_headers)
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"

    reopened = client.post(f"/api/tickets/{ticket_id}/reopen", headers=trainer_headers)
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "OPEN"


def test_reopening_an_old_ticket_starts_a_new_expiration_window(client, db_session):
    ticket = Ticket(
        class_id=db_session.info["class_id"],
        student_id=db_session.info["student_id"],
        question="Old but still relevant?",
        status="RESOLVED",
        created_at=datetime.now(UTC) - timedelta(days=10),
    )
    db_session.add(ticket)
    db_session.commit()
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")

    reopened = client.post(f"/api/tickets/{ticket.id}/reopen", headers=trainer_headers)
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "OPEN"

    listed = client.get("/api/tickets?status=OPEN", headers=trainer_headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == str(ticket.id)
    assert listed.json()["items"][0]["status"] == "OPEN"


def test_student_can_reopen_their_resolved_ticket(client, db_session):
    created = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Still need help.",
    })
    ticket_id = created.json()["id"]
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")
    student_headers = _headers(db_session.info["student_id"])

    client.post(f"/api/tickets/{ticket_id}/replies", headers=trainer_headers,
                json={"message": "First answer."})
    assert client.post(f"/api/tickets/{ticket_id}/resolve",
                       headers=student_headers).json()["status"] == "RESOLVED"
    reopened = client.post(f"/api/tickets/{ticket_id}/reopen", headers=student_headers)
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "ANSWERED"


def test_invalid_ticket_status_transitions_are_rejected(client, db_session):
    created = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Question?",
    })
    ticket_id = created.json()["id"]
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")

    assert client.post(f"/api/tickets/{ticket_id}/reopen",
                       headers=trainer_headers).status_code == 409
    assert client.post(f"/api/tickets/{ticket_id}/resolve",
                       headers=trainer_headers).status_code == 200
    assert client.post(f"/api/tickets/{ticket_id}/resolve",
                       headers=trainer_headers).status_code == 409
    assert client.post(f"/api/tickets/{uuid.uuid4()}/resolve",
                       headers=trainer_headers).status_code == 404


def test_student_cannot_create_ticket_for_unenrolled_class(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(
        "/api/tickets",
        headers=_headers(db_session.info["student_id"]),
        json={"class_id": str(db_session.info["other_class_id"]), "question": "Question?"},
    )

    assert response.status_code == 403


def test_student_question_and_selected_agent_answer_are_stored_separately(client, db_session):
    question = "Could you explain why this condition is needed?"
    original_question = "What does list comprehension mean?"
    excerpt = "Only items matching the condition are included."
    response = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]),
        "question": question,
        "original_question": original_question,
        "agent_answer": excerpt,
    })
    assert response.status_code == 201
    ticket = response.json()
    assert ticket["question"] == question
    assert ticket["original_question"] == original_question
    assert ticket["agent_answer"] == excerpt
    db_session.expire_all()
    saved = db_session.get(Ticket, uuid.UUID(ticket["id"]))
    assert saved.question == question
    assert saved.original_question == original_question
    assert saved.agent_answer == excerpt
    assert saved.replies == []
    inbox = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))
    assert inbox.status_code == 200
    assert inbox.json()["items"][0]["question"] == question
    assert inbox.json()["items"][0]["original_question"] == original_question
    assert inbox.json()["items"][0]["agent_answer"] == excerpt


def test_agent_excerpt_is_optional_for_existing_clients(client, db_session):
    response = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Legacy question?",
    })
    assert response.status_code == 201
    assert response.json()["agent_answer"] is None


def test_agent_excerpt_length_is_validated(client, db_session):
    response = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]), json={
        "class_id": str(db_session.info["class_id"]), "question": "Question?",
        "agent_answer": "x" * 10001,
    })
    assert response.status_code == 422
    assert db_session.query(Ticket).count() == 0


def test_student_cannot_reply_to_another_students_ticket(
    client: TestClient, db_session: Session
) -> None:
    created = client.post(
        "/api/tickets",
        headers=_headers(db_session.info["student_id"]),
        json={"class_id": str(db_session.info["class_id"]), "question": "Question?"},
    )
    ticket_id = created.json()["id"]
    other_headers = _headers(db_session.info["other_student_id"])

    assert (
        client.post(
            f"/api/tickets/{ticket_id}/replies",
            headers=other_headers,
            json={"message": "Not my ticket."},
        ).status_code
        == 404
    )


def test_non_student_cannot_create_ticket(client: TestClient, db_session: Session) -> None:
    response = client.post(
        "/api/tickets",
        headers=_headers(db_session.info["trainer_id"], "trainer"),
        json={"class_id": str(db_session.info["class_id"]), "question": "Question?"},
    )

    assert response.status_code == 403


def test_unauthenticated_cannot_create_or_reply(client: TestClient, db_session: Session) -> None:
    assert client.post("/api/tickets", json={
        "class_id": str(db_session.info["class_id"]), "question": "Question?",
    }).status_code == 401
    assert client.post(f"/api/tickets/{uuid.uuid4()}/replies", json={
        "message": "Follow up",
    }).status_code == 401
    assert db_session.query(Ticket).count() == 0


@pytest.mark.parametrize("inactive", ["class", "enrollment"])
def test_inactive_membership_cannot_create(client, db_session, inactive):
    if inactive == "class":
        db_session.get(Class, db_session.info["class_id"]).status = "ARCHIVED"
    else:
        db_session.query(Enrollment).filter_by(
            student_id=db_session.info["student_id"],
        ).one().status = "INACTIVE"
    db_session.commit()
    response = client.post("/api/tickets", headers=_headers(db_session.info["student_id"]),
                           json={"class_id": str(db_session.info["class_id"]), "question": "Why?"})
    assert response.status_code == 403
    assert db_session.query(Ticket).count() == 0


def test_blank_question_and_follow_up_are_rejected(client, db_session):
    headers = _headers(db_session.info["student_id"])
    assert client.post("/api/tickets", headers=headers, json={
        "class_id": str(db_session.info["class_id"]), "question": "  ",
    }).status_code == 422
    created = client.post("/api/tickets", headers=headers, json={
        "class_id": str(db_session.info["class_id"]), "question": "  Why?  ",
    }).json()
    assert created["question"] == "Why?"
    assert client.post(f"/api/tickets/{created['id']}/replies", headers=headers,
                       json={"message": "  "}).status_code == 422
    assert client.post(f"/api/tickets/{created['id']}/replies",
                       headers=_headers(db_session.info["trainer_id"], "trainer"),
                       json={"message": "Teacher answer"}).status_code == 201
    assert db_session.query(TicketReply).count() == 1


@pytest.fixture()
def inbox(db_session):
    trainer = db_session.get(User, db_session.info["trainer_id"])
    outsider = User(full_name="Other Trainer", email="other-trainer@example.com",
                    role_id=trainer.role_id, status="ACTIVE")
    co_trainer = User(full_name="Co Trainer", email="co-trainer@example.com",
                      role_id=trainer.role_id, status="ACTIVE")
    db_session.add_all([outsider, co_trainer])
    db_session.flush()
    foreign_class = Class(code="PRIVATE", name="Private class", trainer_id=outsider.id,
                          semester="HK1", academic_year="2026-2027", status="ACTIVE")
    db_session.add(foreign_class)
    db_session.flush()
    db_session.add(ClassTrainer(class_id=db_session.info["class_id"], trainer_id=co_trainer.id))
    # The last two share a timestamp, so ID is the deterministic tie breaker.
    items = []
    for index, state in enumerate(["OPEN", "ANSWERED", "RESOLVED", "OPEN", "ANSWERED"]):
        ticket = Ticket(id=uuid.UUID(int=index+1), class_id=db_session.info[
            "other_class_id" if index == 3 else "class_id"],
            student_id=db_session.info["student_id"], question=f"Question {index}", status=state,
            created_at=datetime(2026, 11, 1, tzinfo=UTC) + timedelta(minutes=min(index, 3)))
        db_session.add(ticket)
        items.append(ticket)
    private_ticket = Ticket(class_id=foreign_class.id, student_id=db_session.info["other_student_id"],
                            question="Private question", status="OPEN")
    db_session.add(private_ticket)
    db_session.commit()
    return {"items": items, "private_class": foreign_class, "outsider": outsider,
            "co_trainer": co_trainer, "private_ticket": private_ticket}


def test_inbox_only_lists_assigned_classes_newest_first(client, db_session, inbox):
    headers = _headers(db_session.info["trainer_id"], "trainer")
    response = client.get("/api/tickets?page_size=2", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert body["page"] == 1 and body["page_size"] == 2
    assert [item["id"] for item in body["items"]] == [str(t.id) for t in inbox["items"][-1:-3:-1]]
    item = body["items"][0]
    assert item["student_name"] == "Student"
    assert item["class_code"] == "AI81-A"
    assert item["class_name"] == "AI-81 Class A"
    assert item["question"] and item["status"] and item["created_at"]
    all_ids = []
    for page in [1, 2, 3, 4]:
        data = client.get(f"/api/tickets?page={page}&page_size=2", headers=headers).json()
        assert data["total"] == 5
        all_ids.extend(item["id"] for item in data["items"])
        assert len(data["items"]) == {1: 2, 2: 2, 3: 1, 4: 0}[page]
    assert all_ids == [str(t.id) for t in reversed(inbox["items"])]


def test_student_lists_only_own_tickets_newest_first(client, db_session, inbox):
    headers = _headers(db_session.info["student_id"])
    response = client.get("/api/tickets/mine?page_size=2", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert body["page"] == 1 and body["page_size"] == 2
    assert [item["id"] for item in body["items"]] == [
        str(t.id) for t in inbox["items"][-1:-3:-1]
    ]
    assert all(
        item["student_id"] == str(db_session.info["student_id"]) for item in body["items"]
    )


def test_student_ticket_list_filters_class_status_and_includes_replies(
    client, db_session, inbox
):
    headers = _headers(db_session.info["student_id"])
    answered = client.get("/api/tickets/mine?status=ANSWERED", headers=headers).json()
    assert answered["total"] == 2
    assert all(item["status"] == "ANSWERED" for item in answered["items"])

    other_class = client.get(
        f"/api/tickets/mine?class_id={db_session.info['other_class_id']}", headers=headers
    ).json()
    assert other_class["total"] == 1
    assert other_class["items"][0]["class_id"] == str(db_session.info["other_class_id"])

    target = inbox["items"][0]
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")
    assert client.post(
        f"/api/tickets/{target.id}/replies",
        headers=trainer_headers,
        json={"message": "Teacher reply"},
    ).status_code == 201

    listed = client.get(
        f"/api/tickets/mine?class_id={db_session.info['class_id']}", headers=headers
    ).json()
    item = next(item for item in listed["items"] if item["id"] == str(target.id))
    assert [reply["message"] for reply in item["replies"]] == ["Teacher reply"]


def test_my_tickets_rejects_other_roles_and_scopes_each_student(client, db_session, inbox):
    assert client.get("/api/tickets/mine").status_code == 401
    assert client.get(
        "/api/tickets/mine", headers=_headers(db_session.info["trainer_id"], "trainer")
    ).status_code == 403

    other = client.get(
        "/api/tickets/mine", headers=_headers(db_session.info["other_student_id"])
    ).json()
    assert other["total"] == 1
    assert other["items"][0]["id"] == str(inbox["private_ticket"].id)


@pytest.mark.parametrize("state,total", [("OPEN", 2), ("ANSWERED", 2), ("RESOLVED", 1)])
def test_inbox_filters_status_and_class(client, db_session, inbox, state, total):
    headers = _headers(db_session.info["trainer_id"], "trainer")
    data = client.get(f"/api/tickets?status={state}", headers=headers).json()
    assert data["total"] == total
    assert all(item["status"] == state for item in data["items"])
    class_id = str(db_session.info["other_class_id"])
    combined = client.get(f"/api/tickets?status={state}&class_id={class_id}", headers=headers).json()
    assert combined["total"] == (1 if state == "OPEN" else 0)
    assert all(item["class_id"] == class_id and item["status"] == state for item in combined["items"])
    by_class = client.get(f"/api/tickets?class_id={class_id}", headers=headers).json()
    assert by_class["total"] == 1


def test_foreign_class_filter_is_rejected_before_counting(client, db_session, inbox):
    headers = _headers(db_session.info["trainer_id"], "trainer")
    for state in ["", "&status=OPEN", "&status=RESOLVED"]:
        response = client.get(f"/api/tickets?class_id={inbox['private_class'].id}{state}", headers=headers)
        assert response.status_code == 403
        assert "total" not in response.json()
    outsider = client.get("/api/tickets", headers=_headers(inbox["outsider"].id, "trainer")).json()
    assert outsider["total"] == 1
    assert outsider["items"][0]["id"] == str(inbox["private_ticket"].id)
    assert client.get(f"/api/tickets?class_id={db_session.info['class_id']}",
                      headers=_headers(inbox["outsider"].id, "trainer")).status_code == 403


def test_co_trainer_inbox_and_empty_assignment(client, db_session, inbox):
    data = client.get("/api/tickets", headers=_headers(inbox["co_trainer"].id, "trainer")).json()
    assert data["total"] == 4
    assert all(item["class_id"] == str(db_session.info["class_id"]) for item in data["items"])
    db_session.query(ClassTrainer).delete()
    db_session.commit()
    assert client.get("/api/tickets", headers=_headers(inbox["co_trainer"].id, "trainer")).json()["items"] == []


def test_ticket_counts_are_tracked_per_assigned_class(client, db_session, inbox):
    headers = _headers(db_session.info["trainer_id"], "trainer")
    response = client.get("/api/tickets/counts", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["open_total"] == 4
    assert body["completed_total"] == 1
    assert body["items"] == [
        {
            "class_id": str(db_session.info["class_id"]),
            "class_code": "AI81-A",
            "class_name": "AI-81 Class A",
            "class_status": "ACTIVE",
            "open_count": 3,
            "completed_count": 1,
            "expired_count": 0,
        },
        {
            "class_id": str(db_session.info["other_class_id"]),
            "class_code": "AI81-B",
            "class_name": "AI-81 Class B",
            "class_status": "ACTIVE",
            "open_count": 1,
            "completed_count": 0,
            "expired_count": 0,
        },
    ]

    filtered = client.get(
        f"/api/tickets/counts?class_id={db_session.info['class_id']}", headers=headers
    )
    assert filtered.status_code == 200
    assert len(filtered.json()["items"]) == 1
    assert filtered.json()["items"][0]["open_count"] == 3


def test_archived_class_is_hidden_from_counts_inbox_and_student_views(client, db_session, inbox):
    section = db_session.get(Class, db_session.info["class_id"])
    assert section is not None
    section.status = "ARCHIVED"
    db_session.commit()
    trainer_headers = _headers(db_session.info["trainer_id"], "trainer")

    counts = client.get("/api/tickets/counts", headers=trainer_headers).json()
    assert [item["class_code"] for item in counts["items"]] == ["AI81-B"]
    assert counts["open_total"] == 1 and counts["completed_total"] == 0

    # Asking for the archived class directly is rejected, and it never shows up
    # in the unfiltered inbox.
    assert client.get(
        f"/api/tickets?class_id={db_session.info['class_id']}", headers=trainer_headers
    ).status_code == 403
    listed = client.get("/api/tickets", headers=trainer_headers).json()
    assert listed["total"] == 1
    assert [item["class_id"] for item in listed["items"]] == [
        str(db_session.info["other_class_id"])
    ]

    # Students keep their history for active classes only.
    student_headers = _headers(db_session.info["student_id"])
    mine = client.get("/api/tickets/mine", headers=student_headers).json()
    assert [item["class_id"] for item in mine["items"]] == [
        str(db_session.info["other_class_id"])
    ]
    archived_ticket = inbox["items"][0]
    assert client.get(
        f"/api/tickets/{archived_ticket.id}", headers=student_headers
    ).status_code == 404
    assert client.post(
        f"/api/tickets/{archived_ticket.id}/resolve", headers=student_headers
    ).status_code == 404
    assert client.post(
        f"/api/tickets/{archived_ticket.id}/reopen", headers=student_headers
    ).status_code == 404
    assert client.post(
        f"/api/tickets/{archived_ticket.id}/replies",
        headers=student_headers,
        json={"message": "Still there?"},
    ).status_code == 404

    # Trainers cannot read, answer, resolve or reopen archived-class tickets.
    assert client.post(
        f"/api/tickets/{archived_ticket.id}/resolve", headers=trainer_headers
    ).status_code == 403
    assert client.post(
        f"/api/tickets/{archived_ticket.id}/reopen", headers=trainer_headers
    ).status_code == 403
    assert client.post(
        f"/api/tickets/{archived_ticket.id}/replies",
        headers=trainer_headers,
        json={"message": "Follow-up"},
    ).status_code == 403


def test_ticket_counts_follow_resolve_and_reopen(client, db_session, inbox):
    headers = _headers(db_session.info["trainer_id"], "trainer")
    class_id = db_session.info["class_id"]
    open_ticket, _, resolved_ticket, _, _ = inbox["items"][:5]

    assert client.post(f"/api/tickets/{open_ticket.id}/resolve", headers=headers).status_code == 200
    after_resolve = client.get(
        f"/api/tickets/counts?class_id={class_id}", headers=headers
    ).json()["items"][0]
    assert after_resolve["open_count"] == 2
    assert after_resolve["completed_count"] == 2

    assert client.post(f"/api/tickets/{resolved_ticket.id}/reopen", headers=headers).status_code == 200
    after_reopen = client.get(
        f"/api/tickets/counts?class_id={class_id}", headers=headers
    ).json()["items"][0]
    assert after_reopen["open_count"] == 3
    assert after_reopen["completed_count"] == 1


def test_ticket_counts_reject_foreign_class_and_other_roles(client, db_session, inbox):
    headers = _headers(db_session.info["trainer_id"], "trainer")
    foreign = client.get(
        f"/api/tickets/counts?class_id={inbox['private_class'].id}", headers=headers
    )
    assert foreign.status_code == 403
    assert client.get(
        "/api/tickets/counts", headers=_headers(db_session.info["student_id"])
    ).status_code == 403
    assert client.get("/api/tickets/counts").status_code == 401


def test_trainer_cannot_change_status_in_a_foreign_class(client, db_session, inbox):
    headers = _headers(db_session.info["trainer_id"], "trainer")
    private_ticket = inbox["private_ticket"]
    assert client.post(
        f"/api/tickets/{private_ticket.id}/resolve", headers=headers
    ).status_code == 403
    private_ticket.status = "RESOLVED"
    db_session.commit()
    assert client.post(
        f"/api/tickets/{private_ticket.id}/reopen", headers=headers
    ).status_code == 403


def test_inbox_rejects_other_roles_and_invalid_filters(client, db_session):
    assert client.get("/api/tickets").status_code == 401
    assert client.get("/api/tickets", headers=_headers(db_session.info["student_id"])).status_code == 403
    for role_name in ["ADMIN", "TEACHING_ASSISTANT"]:
        role = db_session.query(Role).filter_by(name=role_name).one()
        user = User(full_name=role_name, email=f"{role_name.lower()}@tickets.example.com",
                    role_id=role.id, status="ACTIVE")
        db_session.add(user)
        db_session.commit()
        assert client.get("/api/tickets", headers=_headers(user.id, role_name.lower())).status_code == 403
    headers = _headers(db_session.info["trainer_id"], "trainer")
    for query in ["status=INVALID", "page=0", "page_size=0", "page_size=101", "class_id=invalid"]:
        assert client.get(f"/api/tickets?{query}", headers=headers).status_code == 422


def _old_open_ticket(db_session, *, class_id=None, hours=25, status="OPEN"):
    ticket = Ticket(
        class_id=class_id or db_session.info["class_id"],
        student_id=db_session.info["student_id"],
        question="Unanswered question",
        status=status,
        created_at=datetime.now(UTC) - timedelta(hours=hours),
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


def test_open_ticket_past_sla_escalates_to_backup_trainer(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    ticket = _old_open_ticket(db_session)

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))

    assert response.status_code == 200
    item = next(i for i in response.json()["items"] if i["id"] == str(ticket.id))
    assert item["escalated_to_id"] == str(inbox["co_trainer"].id)
    assert item["escalated_to_name"] == "Co Trainer"
    assert item["escalated_at"] is not None


def test_ticket_inside_sla_is_not_escalated(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    ticket = _old_open_ticket(db_session, hours=1)

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))

    item = next(i for i in response.json()["items"] if i["id"] == str(ticket.id))
    assert item["escalated_to_id"] is None
    assert item["escalated_at"] is None


def test_answered_ticket_past_sla_is_never_escalated(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    ticket = _old_open_ticket(db_session, status="ANSWERED")

    response = client.get(
        "/api/tickets?status=ANSWERED",
        headers=_headers(db_session.info["trainer_id"], "trainer"),
    )

    item = next(i for i in response.json()["items"] if i["id"] == str(ticket.id))
    assert item["escalated_to_id"] is None
    assert item["escalated_at"] is None


def test_ticket_without_backup_trainer_is_not_escalated(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    ticket = _old_open_ticket(db_session, class_id=db_session.info["other_class_id"])

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))

    item = next(i for i in response.json()["items"] if i["id"] == str(ticket.id))
    assert item["escalated_to_id"] is None
    assert item["escalated_at"] is None


def test_escalation_happens_only_once_per_ticket(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    _old_open_ticket(db_session)
    headers = _headers(db_session.info["trainer_id"], "trainer")

    first = client.get("/api/tickets", headers=headers).json()["items"][0]
    assert first["escalated_to_id"] == str(inbox["co_trainer"].id)

    db_session.query(ClassTrainer).filter_by(
        class_id=db_session.info["class_id"], trainer_id=inbox["co_trainer"].id
    ).delete()
    db_session.add(
        ClassTrainer(class_id=db_session.info["class_id"], trainer_id=inbox["outsider"].id)
    )
    db_session.commit()

    second = client.get("/api/tickets", headers=headers).json()["items"][0]
    assert second["escalated_to_id"] == str(inbox["co_trainer"].id)
    assert second["escalated_at"] == first["escalated_at"]


def test_escalation_sla_is_configurable(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    db_session.add(SystemConfig(key="ticket_escalation_sla_hours", value="2"))
    db_session.commit()
    escalated = _old_open_ticket(db_session, hours=3)
    waiting = _old_open_ticket(db_session, hours=1)

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))

    items = {item["id"]: item for item in response.json()["items"]}
    assert items[str(escalated.id)]["escalated_to_id"] == str(inbox["co_trainer"].id)
    assert items[str(waiting.id)]["escalated_to_id"] is None


def test_holiday_pauses_the_escalation_sla(db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    now = datetime(2026, 5, 4, 3, 0, tzinfo=UTC)
    db_session.add(
        Holiday(
            date=date(2026, 5, 3),
            name="Nghỉ lễ",
            type="admin",
            scope="class",
            class_id=db_session.info["class_id"],
        )
    )
    waiting = Ticket(
        class_id=db_session.info["class_id"],
        student_id=db_session.info["student_id"],
        question="Waiting through a holiday",
        status="OPEN",
        created_at=now - timedelta(hours=25),
    )
    escalated = Ticket(
        class_id=db_session.info["class_id"],
        student_id=db_session.info["student_id"],
        question="Past the holiday-aware SLA",
        status="OPEN",
        created_at=now - timedelta(hours=49),
    )
    db_session.add_all([waiting, escalated])
    db_session.commit()

    assert escalate_stale_tickets(db_session, now=now) == 1

    db_session.refresh(waiting)
    db_session.refresh(escalated)
    assert waiting.escalated_to_id is None
    assert escalated.escalated_to_id == inbox["co_trainer"].id


def test_reopened_ticket_uses_a_fresh_escalation_window(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    ticket = _old_open_ticket(db_session, hours=24 * 8)
    ticket.reopened_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))

    item = next(i for i in response.json()["items"] if i["id"] == str(ticket.id))
    assert item["escalated_to_id"] is None


def test_escalation_sla_is_capped_below_ticket_expiration(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    db_session.add(SystemConfig(key="ticket_escalation_sla_hours", value="200"))
    db_session.commit()
    escalated = _old_open_ticket(db_session, hours=167.5)
    expired = _old_open_ticket(db_session, hours=169)

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))

    items = {item["id"]: item for item in response.json()["items"]}
    assert items[str(escalated.id)]["escalated_to_id"] == str(inbox["co_trainer"].id)
    assert items[str(expired.id)]["status"] == "EXPIRED"
    assert items[str(expired.id)]["escalated_to_id"] is None


def test_ticket_list_does_not_query_a_user_per_escalated_ticket(client, db_session, inbox):
    """Regression: escalated_to_name must not lazy-load one user row per ticket."""
    from sqlalchemy import event

    db_session.query(Ticket).delete()
    db_session.commit()
    class_id = db_session.info["class_id"]
    for index in range(5):
        db_session.add(
            Ticket(
                class_id=class_id,
                student_id=db_session.info["student_id"],
                question=f"Escalated {index}",
                status="OPEN",
                created_at=datetime.now(UTC) - timedelta(hours=48),
                escalated_to_id=inbox["co_trainer"].id,
                escalated_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    db_session.commit()

    statements: list[str] = []
    engine = db_session.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 5
    user_selects = [s for s in statements if s.lstrip().upper().startswith("SELECT USER.")]
    assert len(user_selects) <= 1, f"expected at most one user SELECT, got {len(user_selects)}"


def test_reading_one_ticket_only_sweeps_its_own_class(client, db_session, inbox):
    """Regression: a single ticket read must not sweep every stale ticket in the DB."""
    from sqlalchemy import event

    db_session.query(Ticket).delete()
    db_session.commit()
    own_class = db_session.info["class_id"]
    other_class = db_session.info["other_class_id"]
    own = Ticket(
        class_id=own_class,
        student_id=db_session.info["student_id"],
        question="Own class",
        status="OPEN",
        created_at=datetime.now(UTC) - timedelta(hours=48),
    )
    db_session.add(own)
    for index in range(20):
        db_session.add(
            Ticket(
                class_id=other_class,
                student_id=db_session.info["other_student_id"],
                question=f"Other class {index}",
                status="OPEN",
                created_at=datetime.now(UTC) - timedelta(hours=48),
            )
        )
    db_session.commit()

    statements: list[str] = []
    engine = db_session.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        response = client.get(
            f"/api/tickets/{own.id}", headers=_headers(db_session.info["student_id"])
        )
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert response.status_code == 200
    sweep_selects = [s for s in statements if "FROM ticket" in s and "escalated_at IS NULL" in s]
    assert len(sweep_selects) == 1
    assert "CLASS_ID IN" in sweep_selects[0].upper()
