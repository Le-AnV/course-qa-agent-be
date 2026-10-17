"""HTTP tests for Admin-only AI-105 settings and connection endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import Settings, settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.services.email_service import EmailDeliveryError
from app.services.llm_service import LlmConnectionError

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
        ("POST", "/api/admin/email/test"),
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


def test_admin_can_send_test_email_to_their_login_address(api_client, monkeypatch):
    """Send the SMTP test message to the currently authenticated Admin."""

    recipients = []

    def record_test_email(recipient, config):
        """Record the recipient and return a successful test result."""

        recipients.append(recipient)
        return {"success": True, "message": "Test email sent successfully"}

    monkeypatch.setattr("app.routers.admin.router.send_test_email", record_test_email)

    response = api_client.post("/api/admin/email/test", headers=admin_headers(api_client))

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "Test email sent successfully",
    }
    assert recipients == [settings.bootstrap_admin_email.lower()]


def test_admin_connection_endpoints_return_safe_failure_results(api_client, monkeypatch):
    """Convert provider failures to clear results without leaking internals."""

    def fail_llm_connection(config):
        """Simulate an unavailable Gemini connection with a safe message."""

        raise LlmConnectionError("Gemini connection failed")

    def fail_email_delivery(recipient, config):
        """Simulate a failed SMTP send without exposing provider details."""

        raise EmailDeliveryError("Test email could not be sent")

    monkeypatch.setattr("app.routers.admin.router.check_llm_connection", fail_llm_connection)
    monkeypatch.setattr("app.routers.admin.router.send_test_email", fail_email_delivery)
    headers = admin_headers(api_client)

    llm_response = api_client.post("/api/admin/llm/test-connection", headers=headers)
    email_response = api_client.post("/api/admin/email/test", headers=headers)

    assert llm_response.json() == {
        "success": False,
        "message": "Gemini connection failed",
    }
    assert email_response.json() == {
        "success": False,
        "message": "Test email could not be sent",
    }
    assert llm_response.status_code == 200
    assert email_response.status_code == 200
