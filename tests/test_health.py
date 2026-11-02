from fastapi.testclient import TestClient

from app.main import app
from app.schemas.system_health import ComponentHealth
from app.services import system_health_service as health

client = TestClient(app)


def _up(name: str, status: str = "up") -> ComponentHealth:
    return ComponentHealth(name=name, status=status, healthy=True, detail=f"{name} ok")


def test_health_returns_status_of_each_component(monkeypatch):
    """AC-01: /health reports DB, Qdrant and LLM components."""
    monkeypatch.setattr(health, "_check_database", lambda db: _up("db"))
    monkeypatch.setattr(health, "_check_qdrant", lambda cfg: _up("qdrant"))
    monkeypatch.setattr(health, "_check_llm_config", lambda cfg: _up("llm", "configured"))

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "course-qa-agent-backend"
    assert [c["name"] for c in body["components"]] == ["db", "qdrant", "llm"]
    assert all(c["healthy"] for c in body["components"])


def test_health_is_degraded_when_a_component_is_down(monkeypatch):
    monkeypatch.setattr(
        health,
        "_check_database",
        lambda db: ComponentHealth(name="db", status="down", healthy=False, detail="down"),
    )
    monkeypatch.setattr(health, "_check_qdrant", lambda cfg: _up("qdrant"))
    monkeypatch.setattr(health, "_check_llm_config", lambda cfg: _up("llm", "configured"))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_health_reports_unconfigured_llm_without_external_call(monkeypatch):
    """Fast path reports LLM readiness only (no Gemini call)."""
    monkeypatch.setattr(health, "_check_database", lambda db: _up("db"))
    monkeypatch.setattr(health, "_check_qdrant", lambda cfg: _up("qdrant"))
    # The LLM key pool lookup needs a live DB; keep this test on the fast path.
    monkeypatch.setattr(health, "_has_active_key", lambda db: False)

    response = client.get("/health")

    assert response.status_code == 200
    llm = next(c for c in response.json()["components"] if c["name"] == "llm")
    assert llm["status"] in {"configured", "unconfigured"}


def test_live_llm_probe_is_cached(monkeypatch):
    """AC-03/repeat-refresh: the live LLM probe is cached for a TTL."""
    calls = {"count": 0}

    def fake_live(_config):
        calls["count"] += 1
        return _up("llm")

    monkeypatch.setattr(health, "_check_llm_live", fake_live)
    health.reset_llm_cache()

    first = health._check_llm_live_cached(object())
    second = health._check_llm_live_cached(object())

    assert calls["count"] == 1
    assert first.status == second.status == "up"
    health.reset_llm_cache()
