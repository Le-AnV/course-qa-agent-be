"""HTTP tests for the AI-109 admin system health endpoint."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.schemas.system_health import ComponentHealth
from app.services import system_health_service as health

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
        json={"email": settings.bootstrap_admin_email, "password": settings.bootstrap_admin_password},
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


def _up(name: str) -> ComponentHealth:
    return ComponentHealth(name=name, status="up", healthy=True, detail=f"{name} ok")


def test_system_health_requires_admin(api_client, db_session):
    student = student_headers(api_client, db_session)
    path = "/api/admin/system/health"
    assert api_client.get(path).status_code == 401
    assert api_client.get(path, headers=student).status_code == 403


def test_system_health_returns_component_status(api_client, monkeypatch):
    """AC-01/AC-02: admin sees a green/red status for each component."""
    h = admin_headers(api_client)
    monkeypatch.setattr(health, "_check_qdrant", lambda cfg: _up("qdrant"))
    monkeypatch.setattr(health, "_check_llm_live_cached", lambda cfg: _up("llm"))

    res = api_client.get("/api/admin/system/health", headers=h)

    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "course-qa-agent-backend"
    assert body["status"] == "ok"
    assert [c["name"] for c in body["components"]] == ["db", "qdrant", "llm"]
    assert all(c["healthy"] for c in body["components"])


def test_system_health_degrades_when_a_dependency_is_down(api_client, monkeypatch):
    h = admin_headers(api_client)
    monkeypatch.setattr(
        health,
        "_check_qdrant",
        lambda cfg: ComponentHealth(
            name="qdrant", status="down", healthy=False, detail="Qdrant unreachable"
        ),
    )
    monkeypatch.setattr(health, "_check_llm_live_cached", lambda cfg: _up("llm"))

    res = api_client.get("/api/admin/system/health", headers=h)

    assert res.status_code == 200
    assert res.json()["status"] == "degraded"
