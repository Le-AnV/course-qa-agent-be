"""Personal profile endpoints (AI-29: AC-01 view, AC-02 change password, AC-03 rename).

Uses in-memory SQLite (StaticPool) so CI stays DB-free. Covers:
- GET /api/auth/me shows display name + email (AC-01),
- PATCH /api/auth/profile renames and /me reflects it immediately (AC-03),
- PATCH rejects empty / overlong names (422),
- POST /api/auth/change-password happy path: old stops, new works (AC-02),
- wrong old password shares the generic 401 message,
- short or identical new password is rejected (422).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.seed import seed_database
from app.main import app

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod

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
        yield db
    finally:
        db.close()
        engine.dispose()
    reset_engine()


@pytest.fixture()
def client(db_session):
    def _override():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed():
    SessionLocal = get_session_local(TEST_DB_URL)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()


def _admin_headers(client):
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@academic.edu.vn", "password": "Admin@123"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_me_shows_display_name_and_email(client):
    _seed()
    me = client.get("/api/auth/me", headers=_admin_headers(client))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["username"] == "System Administrator"
    assert body["email"] == "admin@academic.edu.vn"


def test_profile_rename_reflects_in_me(client):
    _seed()
    headers = _admin_headers(client)

    res = client.patch("/api/auth/profile",
                       json={"full_name": "Nguyen Van A"}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["username"] == "Nguyen Van A"

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "Nguyen Van A"


def test_profile_rename_rejects_bad_names(client):
    _seed()
    headers = _admin_headers(client)

    res = client.patch("/api/auth/profile", json={"full_name": "   "}, headers=headers)
    assert res.status_code == 422

    res = client.patch("/api/auth/profile", json={"full_name": "x" * 101}, headers=headers)
    assert res.status_code == 422


def test_change_password_happy_path(client):
    _seed()
    headers = _admin_headers(client)

    res = client.post(
        "/api/auth/change-password",
        json={"old_password": "Admin@123", "new_password": "N3w-Secure-Pass"},
        headers=headers,
    )
    assert res.status_code == 200, res.text

    # Old password stops working, new one works — both via the DB login.
    bad = client.post(
        "/api/auth/login",
        json={"email": "admin@academic.edu.vn", "password": "Admin@123"},
    )
    assert bad.status_code == 401
    good = client.post(
        "/api/auth/login",
        json={"email": "admin@academic.edu.vn", "password": "N3w-Secure-Pass"},
    )
    assert good.status_code == 200, good.text


def test_change_password_wrong_old_is_generic_401(client):
    _seed()
    headers = _admin_headers(client)

    res = client.post(
        "/api/auth/change-password",
        json={"old_password": "NotThePassword", "new_password": "N3w-Secure-Pass"},
        headers=headers,
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password."


def test_change_password_rejects_weak_new(client):
    _seed()
    headers = _admin_headers(client)

    short = client.post(
        "/api/auth/change-password",
        json={"old_password": "Admin@123", "new_password": "short"},
        headers=headers,
    )
    assert short.status_code == 422

    same = client.post(
        "/api/auth/change-password",
        json={"old_password": "Admin@123", "new_password": "Admin@123"},
        headers=headers,
    )
    assert same.status_code == 422
