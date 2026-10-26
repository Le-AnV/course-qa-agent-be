"""HTTP tests for Admin-only AI-105 settings and connection endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import Settings, settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.services.llm import LlmConnectionError

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
    """Create one student account via the admin, then log in as them."""
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


def test_admin_settings_requires_admin_role(api_client, db_session):
    """Reject unauthenticated and non-admin callers from every AI-105 route."""

    endpoints = [
        ("GET", "/api/admin/settings"),
        ("POST", "/api/admin/llm/test-connection"),
        ("GET", "/api/admin/system/health"),
    ]
    student = student_headers(api_client, db_session)
    for method, path in endpoints:
        assert api_client.request(method, path).status_code == 401
        assert api_client.request(method, path, headers=student).status_code == 403


def test_admin_settings_returns_safe_configuration(api_client, monkeypatch):
    """Return settings metadata to an Admin without either configured secret."""

    monkeypatch.setattr(
        "app.routers.admin.router.settings",
        Settings(
            _env_file=None,
            google_api_key="test-google-key",
            smtp_sender_email="sender@example.com",
            smtp_app_password="test-smtp-password",
        ),
    )

    response = api_client.get("/api/admin/settings", headers=admin_headers(api_client))

    assert response.status_code == 200
    payload = response.json()
    assert payload["llm"]["model"]
    assert "api_key_configured" in payload["llm"]
    assert "app_password_configured" in payload["email"]
    assert "smtp_app_password" not in response.text
    assert "google_api_key" not in response.text


def test_admin_can_check_llm_connection(api_client, monkeypatch):
    """Expose the safe result from the Gemini connection-check service."""

    monkeypatch.setattr(
        "app.routers.admin.router.check_llm_connection",
        lambda config: {"success": True, "message": "Gemini connection successful"},
    )

    response = api_client.post(
        "/api/admin/llm/test-connection",
        headers=admin_headers(api_client),
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "Gemini connection successful",
    }


def test_admin_llm_connection_returns_safe_failure_result(api_client, monkeypatch):
    """Convert provider failures to clear results without leaking internals."""

    def fail_llm_connection(config):
        """Simulate an unavailable Gemini connection with a safe message."""

        raise LlmConnectionError("Gemini connection failed")

    monkeypatch.setattr("app.routers.admin.router.check_llm_connection", fail_llm_connection)

    response = api_client.post(
        "/api/admin/llm/test-connection", headers=admin_headers(api_client)
    )

    assert response.json() == {
        "success": False,
        "message": "Gemini connection failed",
    }
    assert response.status_code == 200

# --- Gemini key pool (admin-managed keys + failover status) ---

from types import SimpleNamespace

from app.services import gemini_key_service


class _FakeGeminiModels:
    def __init__(self, names):
        self._names = names

    def list(self):
        return [SimpleNamespace(name=name) for name in self._names]


class _FakeGeminiClient:
    def __init__(self, api_key=None, **_):
        self.api_key = api_key
        self.names = [
            "models/gemini-3.5-flash-lite",
            "models/gemini-flash-lite-latest",
            "models/gemini-embedding-2",
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    @property
    def models(self):
        return _FakeGeminiModels(self.names)


def _patch_gemini_client(monkeypatch):
    monkeypatch.setattr(gemini_key_service.genai, "Client", _FakeGeminiClient)


def test_admin_can_manage_a_gemini_key_lifecycle(api_client, monkeypatch):
    _patch_gemini_client(monkeypatch)
    headers = admin_headers(api_client)

    created = api_client.post(
        "/api/admin/llm/keys",
        json={
            "name": "Personal",
            "api_key": "AIzaSyAdminKey1234",
            "account_label": "trung@gmail.com",
            "project_label": "course-qa-dev",
        },
        headers=headers,
    )

    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["masked_key"] == "AIza...1234"
    assert payload["status"] == "active"
    assert "AIzaSyAdminKey1234" not in created.text
    key_id = payload["id"]

    listed = api_client.get("/api/admin/llm/keys", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["active_count"] == 1
    assert listed.json()["keys"][0]["name"] == "Personal"

    paused = api_client.patch(
        f"/api/admin/llm/keys/{key_id}",
        json={"status": "paused"},
        headers=headers,
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    tested = api_client.post(f"/api/admin/llm/keys/{key_id}/test", headers=headers)
    assert tested.status_code == 200
    assert tested.json()["success"] is True

    deleted = api_client.delete(f"/api/admin/llm/keys/{key_id}", headers=headers)
    assert deleted.status_code == 204
    assert api_client.get("/api/admin/llm/keys", headers=headers).json()["keys"] == []


def test_admin_can_clear_gemini_key_labels(api_client, monkeypatch):
    _patch_gemini_client(monkeypatch)
    headers = admin_headers(api_client)
    created = api_client.post(
        "/api/admin/llm/keys",
        json={
            "name": "Labeled",
            "api_key": "AIzaSyLabelKey9012",
            "account_label": "trung@gmail.com",
            "project_label": "course-qa-dev",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    key_id = created.json()["id"]

    renamed = api_client.patch(
        f"/api/admin/llm/keys/{key_id}",
        json={"name": "Renamed"},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed"
    assert renamed.json()["account_label"] == "trung@gmail.com"
    assert renamed.json()["project_label"] == "course-qa-dev"

    cleared = api_client.patch(
        f"/api/admin/llm/keys/{key_id}",
        json={"account_label": None, "project_label": None},
        headers=headers,
    )

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["name"] == "Renamed"
    assert cleared.json()["account_label"] is None
    assert cleared.json()["project_label"] is None


def test_admin_can_toggle_auto_switch(api_client):
    headers = admin_headers(api_client)

    listed = api_client.get("/api/admin/llm/keys", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["auto_switch"] is True

    disabled = api_client.patch(
        "/api/admin/llm/auto-switch",
        json={"enabled": False},
        headers=headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert api_client.get("/api/admin/llm/keys", headers=headers).json()["auto_switch"] is False

    enabled = api_client.patch(
        "/api/admin/llm/auto-switch",
        json={"enabled": True},
        headers=headers,
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True


def test_admin_can_choose_an_available_chat_model(api_client, monkeypatch):
    _patch_gemini_client(monkeypatch)
    headers = admin_headers(api_client)
    api_client.post(
        "/api/admin/llm/keys",
        json={"name": "Primary", "api_key": "AIzaSyModelKey5678"},
        headers=headers,
    )

    model_config = api_client.get("/api/admin/llm/models", headers=headers)
    assert model_config.status_code == 200
    assert "models/gemini-3.5-flash-lite" in model_config.json()["available_models"]

    updated = api_client.patch(
        "/api/admin/llm/model",
        json={"model": "models/gemini-3.5-flash-lite"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["model"] == "models/gemini-3.5-flash-lite"


def test_gemini_key_routes_require_admin(api_client, db_session):
    student = student_headers(api_client, db_session)
    for method, path in [
        ("GET", "/api/admin/llm/keys"),
        ("POST", "/api/admin/llm/keys"),
        ("PATCH", "/api/admin/llm/auto-switch"),
        ("GET", "/api/admin/llm/models"),
        ("PATCH", "/api/admin/llm/model"),
    ]:
        assert api_client.request(method, path, json={}).status_code == 401
        assert api_client.request(method, path, json={}, headers=student).status_code == 403
