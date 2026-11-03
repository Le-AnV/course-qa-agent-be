from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.course import Class, ClassTrainer, Enrollment
from app.models.system_config import SystemConfig
from app.models.ticket import Ticket, TicketReply
from app.models.user import Role, User


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


def test_old_open_tickets_expire_in_trainer_inbox(client, db_session):
    from datetime import UTC, datetime, timedelta

    ticket = Ticket(class_id=db_session.info["class_id"], student_id=db_session.info["student_id"],
                    question="Old question?", created_at=datetime.now(UTC) - timedelta(days=4))
    db_session.add(ticket)
    db_session.commit()

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))
    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "EXPIRED"


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


def test_escalation_sla_is_capped_below_ticket_expiration(client, db_session, inbox):
    db_session.query(Ticket).delete()
    db_session.commit()
    db_session.add(SystemConfig(key="ticket_escalation_sla_hours", value="96"))
    db_session.commit()
    escalated = _old_open_ticket(db_session, hours=71.5)
    expired = _old_open_ticket(db_session, hours=73)

    response = client.get("/api/tickets", headers=_headers(db_session.info["trainer_id"], "trainer"))

    items = {item["id"]: item for item in response.json()["items"]}
    assert items[str(escalated.id)]["escalated_to_id"] == str(inbox["co_trainer"].id)
    assert items[str(expired.id)]["status"] == "EXPIRED"
    assert items[str(expired.id)]["escalated_to_id"] is None
