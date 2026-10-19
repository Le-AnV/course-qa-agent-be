"""Class information / class page (Phase B).

Covers the API contract:
- GET   /api/classes/{id}/info  -> all-empty defaults before anything is saved
- PATCH /api/classes/{id}/info  -> trainers/admins write, "" clears a field
- links are validated, long values rejected
- enrolled students read the class page; outsiders and other trainers don't write
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.models.course import Enrollment

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)

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
        yield db
    finally:
        db.close()
        engine.dispose()
    reset_engine()


@pytest.fixture()
def api_client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def admin_headers(client: TestClient) -> dict:
    res = client.post(
        "/api/auth/login",
        json={
            "email": settings.bootstrap_admin_email,
            "password": settings.bootstrap_admin_password,
        },
    )
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def make_user(client: TestClient, admin: dict, email: str, name: str, role: str) -> str:
    res = client.post(
        "/api/admin/users",
        json={"username": name, "email": email, "role": role},
        headers=admin,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def token_for(db_session: Session, email: str, role: str) -> dict:
    from app.core.security import create_access_token
    from app.models.user import User

    user = db_session.query(User).filter(User.email == email).first()
    return {"Authorization": f"Bearer {create_access_token(str(user.id), role)}"}


def make_class(client: TestClient, admin: dict, code: str, trainer_id: str) -> str:
    res = client.post(
        "/api/admin/classes",
        json={"code": code, "trainer_id": trainer_id, "credits": 3},
        headers=admin,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def enroll(db_session: Session, class_id: str, student_id: str) -> None:
    import uuid

    db_session.add(
        Enrollment(
            class_id=uuid.UUID(class_id), student_id=uuid.UUID(student_id), status="ACTIVE"
        )
    )
    db_session.commit()


def test_info_is_empty_until_it_is_filled_in(api_client):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)

    res = api_client.get(f"/api/classes/{class_id}/info", headers=admin)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["class_id"] == class_id
    assert body["description"] is None
    assert body["meeting_url"] is None
    assert body["group_url"] is None
    assert body["schedule_note"] is None
    assert body["note"] is None


def test_primary_trainer_fills_the_class_page(api_client, db_session):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)
    headers = token_for(db_session, "t1@gmail.com", "trainer")

    res = api_client.patch(
        f"/api/classes/{class_id}/info",
        json={
            "description": "  Web development with FastAPI.  ",
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "group_url": "https://zalo.me/g/abc123",
            "schedule_note": "Mon & Wed 19:00-21:00",
            "note": "Group registration opens in week 3.",
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["description"] == "Web development with FastAPI."
    assert body["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert body["schedule_note"] == "Mon & Wed 19:00-21:00"

    # Omitted fields keep their value; "" clears one.
    partial = api_client.patch(
        f"/api/classes/{class_id}/info", json={"schedule_note": ""}, headers=headers
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["schedule_note"] is None
    assert partial.json()["group_url"] == "https://zalo.me/g/abc123"

    # Persisted for the next reader.
    again = api_client.get(f"/api/classes/{class_id}/info", headers=admin).json()
    assert again["note"] == "Group registration opens in week 3."
    assert again["schedule_note"] is None


def test_links_must_be_http_urls(api_client, db_session):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)
    headers = token_for(db_session, "t1@gmail.com", "trainer")

    bad = api_client.patch(
        f"/api/classes/{class_id}/info",
        json={"meeting_url": "meet.google.com/abc"},
        headers=headers,
    )
    assert bad.status_code == 400, bad.text
    assert "http" in bad.json()["detail"]

    current = api_client.get(f"/api/classes/{class_id}/info", headers=admin).json()
    assert current["meeting_url"] is None


def test_over_long_values_are_rejected(api_client, db_session):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)
    headers = token_for(db_session, "t1@gmail.com", "trainer")

    too_long = api_client.patch(
        f"/api/classes/{class_id}/info", json={"description": "x" * 2001}, headers=headers
    )
    assert too_long.status_code == 422


def test_enrolled_students_read_but_never_write(api_client, db_session):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    enrolled = make_user(api_client, admin, "s1@gmail.com", "Student One", "student")
    make_user(api_client, admin, "s2@gmail.com", "Student Two", "student")
    class_id = make_class(api_client, admin, "CS101-A", trainer)
    enroll(db_session, class_id, enrolled)

    api_client.patch(
        f"/api/classes/{class_id}/info",
        json={"description": "Read me"},
        headers=token_for(db_session, "t1@gmail.com", "trainer"),
    )

    read = api_client.get(
        f"/api/classes/{class_id}/info", headers=token_for(db_session, "s1@gmail.com", "student")
    )
    assert read.status_code == 200, read.text
    assert read.json()["description"] == "Read me"

    write = api_client.patch(
        f"/api/classes/{class_id}/info",
        json={"description": "hacked"},
        headers=token_for(db_session, "s1@gmail.com", "student"),
    )
    assert write.status_code == 403

    blocked = api_client.get(
        f"/api/classes/{class_id}/info", headers=token_for(db_session, "s2@gmail.com", "student")
    )
    assert blocked.status_code == 403


def test_another_trainer_cannot_write_the_class_page(api_client, db_session):
    admin = admin_headers(api_client)
    owner = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    make_user(api_client, admin, "t2@gmail.com", "Trainer Two", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", owner)

    res = api_client.patch(
        f"/api/classes/{class_id}/info",
        json={"description": "not mine"},
        headers=token_for(db_session, "t2@gmail.com", "trainer"),
    )
    assert res.status_code == 403
