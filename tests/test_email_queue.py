"""Email outbox drain state (AC-09).

The admin "Process queue now" button must show a running state that survives a
page reload: `GET /api/admin/email-queue/stats` reports live drain progress, so
the UI can disable the button and poll until the drain finishes.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.services import email_service

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


@pytest.fixture(autouse=True)
def _reset_drain_state():
    email_service._DRAIN_STATE.update(
        draining=False, started_at=None, total=0, done=0, sent=0, failed=0
    )
    yield
    email_service._DRAIN_STATE.update(
        draining=False, started_at=None, total=0, done=0, sent=0, failed=0
    )


def _queue(db: Session, count: int) -> None:
    for i in range(count):
        email_service.queue_email(db, f"student{i}@example.com", f"Subject {i}", "Body")


def test_stats_report_no_drain_at_rest(db_session: Session):
    stats = email_service.queue_stats(db_session)
    assert stats["draining"] is False
    assert stats["drain_total"] == 0
    assert stats["drain_done"] == 0


def test_stats_expose_live_progress_while_draining(db_session: Session, monkeypatch):
    _queue(db_session, 4)
    release = threading.Event()
    started = threading.Event()

    def slow_send(*args, **kwargs):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(email_service, "_send_one", slow_send)
    monkeypatch.setattr(
        email_service, "effective_email_config", lambda db: {"smtp_rate_per_minute": 30, "email_dry_run": False}
    )
    monkeypatch.setattr(email_service, "_smtp_credentials", lambda db: ("host", 1, "sender", "pw"))

    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(email_service.process_queue(db_session)))
    worker.start()
    try:
        assert started.wait(timeout=5), "drain never reached the first send"
        live = email_service.queue_stats(db_session)
        assert live["draining"] is True
        assert live["drain_total"] == 4
        assert live["drain_started_at"] is not None
    finally:
        release.set()
        worker.join(timeout=10)

    assert result["sent"] == 4
    after = email_service.queue_stats(db_session)
    assert after["draining"] is False
    assert after["drain_total"] == 4
    assert after["drain_done"] == 4


def test_stats_endpoint_returns_drain_fields(api_client: TestClient):
    res = api_client.get("/api/admin/email-queue/stats", headers=admin_headers(api_client))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draining"] is False
    assert {"draining", "drain_total", "drain_done", "drain_sent", "drain_failed"}.issubset(body)


def test_concurrent_drain_reports_busy_without_second_run(db_session: Session, monkeypatch):
    _queue(db_session, 1)
    release = threading.Event()
    started = threading.Event()

    def slow_send(*args, **kwargs):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(email_service, "_send_one", slow_send)
    monkeypatch.setattr(
        email_service, "effective_email_config", lambda db: {"smtp_rate_per_minute": 30, "email_dry_run": False}
    )
    monkeypatch.setattr(email_service, "_smtp_credentials", lambda db: ("host", 1, "sender", "pw"))

    worker = threading.Thread(target=lambda: email_service.process_queue(db_session))
    worker.start()
    try:
        assert started.wait(timeout=5)
        second = email_service.process_queue(db_session)
        assert second["busy"] is True
        assert second["processed"] == 0
    finally:
        release.set()
        worker.join(timeout=10)

    time.sleep(0.05)
    assert email_service.queue_stats(db_session)["draining"] is False
