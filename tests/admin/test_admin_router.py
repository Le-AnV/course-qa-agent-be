"""HTTP tests for Admin-only AI-105 settings and connection endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.services.auth import create_access_token
from app.services.email_service import EmailDeliveryError
from app.services.llm_service import LlmConnectionError

TEST_JWT_SECRET = "test-only-admin-router-jwt-secret-32-bytes"


@pytest.fixture
def api_client(monkeypatch):
    """Use a sufficiently long test-only JWT key for endpoint authorization."""

    monkeypatch.setattr("app.services.auth.SECRET_KEY", TEST_JWT_SECRET)
    monkeypatch.setattr("app.dependencies.auth.SECRET_KEY", TEST_JWT_SECRET)
    return TestClient(app)


def admin_headers(role="admin", identifier="admin@academic.edu.vn"):
    """Build authorization headers for a signed test user token."""

    token = create_access_token({"sub": identifier, "role": role})
    return {"Authorization": f"Bearer {token}"}


def test_admin_settings_requires_admin_role(api_client):
    """Reject unauthenticated and non-admin callers from every AI-105 route."""

    endpoints = [
        ("GET", "/api/admin/settings"),
        ("POST", "/api/admin/llm/test-connection"),
        ("POST", "/api/admin/email/test"),
    ]
    for method, path in endpoints:
        assert api_client.request(method, path).status_code == 401
        for role in ("student", "trainer"):
            response = api_client.request(
                method,
                path,
                headers=admin_headers(role=role, identifier=f"{role}@example.com"),
            )
            assert response.status_code == 403


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

    response = api_client.get("/api/admin/settings", headers=admin_headers())

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
        headers=admin_headers(),
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

    monkeypatch.setattr(
        "app.routers.admin.router.send_test_email", record_test_email
    )
    headers = admin_headers(identifier="admin@example.com")

    response = api_client.post("/api/admin/email/test", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "Test email sent successfully",
    }
    assert recipients == ["admin@example.com"]


def test_admin_connection_endpoints_return_safe_failure_results(api_client, monkeypatch):
    """Convert provider failures to clear results without leaking internals."""

    def fail_llm_connection(config):
        """Simulate an unavailable Gemini connection with a safe message."""

        raise LlmConnectionError("Gemini connection failed")

    def fail_email_delivery(recipient, config):
        """Simulate a failed SMTP send without exposing provider details."""

        raise EmailDeliveryError("Test email could not be sent")

    monkeypatch.setattr(
        "app.routers.admin.router.check_llm_connection", fail_llm_connection
    )
    monkeypatch.setattr(
        "app.routers.admin.router.send_test_email", fail_email_delivery
    )
    headers = admin_headers()

    llm_response = api_client.post(
        "/api/admin/llm/test-connection", headers=headers
    )
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
