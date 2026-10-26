"""HTTP tests for the Admin-only decisions key pool."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.services import openai_key_service

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)
    monkeypatch.setattr(settings, "openai_api_key", None)

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
        try:
            yield db_session
        finally:
            pass

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


def student_headers(client: TestClient, db_session) -> dict:
    admin = admin_headers(client)
    email = "student01@gmail.com"
    client.post(
        "/api/admin/users",
        json={"username": "Student One", "email": email, "role": "student"},
        headers=admin,
    )
    from app.core.security import create_access_token
    from app.models.user import User

    user = db_session.query(User).filter(User.email == email).first()
    token = create_access_token(str(user.id), "student")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _patch_openai_validation(monkeypatch):
    monkeypatch.setattr(
        openai_key_service, "validate_key", lambda api_key: ["gpt-6-luna"]
    )


def test_admin_can_manage_a_decisions_key_lifecycle(api_client):
    headers = admin_headers(api_client)

    created = api_client.post(
        "/api/admin/decisions/keys",
        json={"name": "Decisions", "api_key": "sk-admin-decisions-1234"},
        headers=headers,
    )

    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["masked_key"] == "sk-a...1234"
    assert payload["status"] == "active"
    assert "sk-admin-decisions-1234" not in created.text
    key_id = payload["id"]

    listed = api_client.get("/api/admin/decisions/keys", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["active_count"] == 1
    assert listed.json()["env_configured"] is False
    assert listed.json()["keys"][0]["name"] == "Decisions"

    paused = api_client.patch(
        f"/api/admin/decisions/keys/{key_id}",
        json={"status": "paused"},
        headers=headers,
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    tested = api_client.post(
        f"/api/admin/decisions/keys/{key_id}/test", headers=headers
    )
    assert tested.status_code == 200
    assert tested.json()["success"] is True

    deleted = api_client.delete(
        f"/api/admin/decisions/keys/{key_id}", headers=headers
    )
    assert deleted.status_code == 204
    assert (
        api_client.get("/api/admin/decisions/keys", headers=headers).json()["keys"] == []
    )


def test_decisions_key_routes_require_admin(api_client, db_session):
    student = student_headers(api_client, db_session)
    missing = uuid.uuid4()
    for method, path in [
        ("GET", "/api/admin/decisions/keys"),
        ("POST", "/api/admin/decisions/keys"),
        ("PATCH", f"/api/admin/decisions/keys/{missing}"),
        ("DELETE", f"/api/admin/decisions/keys/{missing}"),
        ("POST", f"/api/admin/decisions/keys/{missing}/test"),
    ]:
        assert api_client.request(method, path, json={}).status_code == 401
        assert (
            api_client.request(method, path, json={}, headers=student).status_code
            == 403
        )
