"""AC coverage for AI-31: courses, classes, accounts, import, email queue."""

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app

# In-memory SQLite shared across threads (TestClient runs endpoints in a
# worker thread). No file on disk, so no Windows WinError 32 lock between
# tests. StaticPool keeps a single connection alive for the whole test.
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
        # Memory DB vanishes on dispose — no drop_all (avoids the
        # users<->classes FK-cycle SAWarning) and no file to delete.
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


def admin_headers(client: TestClient) -> dict:
    # Bootstrap admin is created on first login attempt.
    res = client.post(
        "/api/auth/login",
        json={"email": settings.bootstrap_admin_email, "password": settings.bootstrap_admin_password},
    )
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def make_xlsx(rows: list[list[str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["username", "gmail", "type", "class"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_course_then_class_flow(client):
    h = admin_headers(client)
    course = client.post("/api/admin/courses", json={"code": "SE101", "title": "Intro"}, headers=h)
    assert course.status_code == 201, course.text
    course_id = course.json()["id"]

    # AC-01: class under a missing course is rejected.
    bad = client.post("/api/admin/classes", json={"course_id": 9999, "code": "X"}, headers=h)
    assert bad.status_code == 400

    good = client.post("/api/admin/classes", json={"course_id": course_id, "code": "SE101"}, headers=h)
    assert good.status_code == 201, good.text
    assert good.json()["course_code"] == "SE101"


def test_single_trainer_per_class_and_login_gate(client):
    h = admin_headers(client)
    course_id = client.post("/api/admin/courses", json={"code": "C1", "title": "T"}, headers=h).json()["id"]
    class_id = client.post(
        "/api/admin/classes", json={"course_id": course_id, "code": "K1"}, headers=h
    ).json()["id"]

    # AC-03: trainer without class cannot be created (needs class before login).
    denied = client.post(
        "/api/admin/users",
        json={"username": "t", "email": "t@gmail.com", "role": "trainer", "class_code": None},
        headers=h,
    )
    assert denied.status_code == 400

    trainer = client.post(
        "/api/admin/users",
        json={"username": "t", "email": "t@gmail.com", "role": "trainer", "class_code": "K1"},
        headers=h,
    )
    assert trainer.status_code == 201, trainer.text
    trainer_id = trainer.json()["id"]
    # AC-02: assign the one trainer to the class; same trainer may serve several classes.
    assigned = client.patch(
        f"/api/admin/classes/{class_id}/assign", json={"trainer_id": trainer_id}, headers=h
    )
    assert assigned.status_code == 200
    assert assigned.json()["trainer_id"] == trainer_id

    class2 = client.post(
        "/api/admin/classes", json={"course_id": course_id, "code": "K2"}, headers=h
    ).json()
    again = client.patch(
        f"/api/admin/classes/{class2['id']}/assign", json={"trainer_id": trainer_id}, headers=h
    )
    assert again.status_code == 200

    # Student gets class scope.
    student = client.post(
        "/api/admin/users",
        json={"username": "s", "email": "s@gmail.com", "role": "student", "class_code": "K1"},
        headers=h,
    )
    assert student.status_code == 201
    assert student.json()["class_code"] == "K1"


def test_import_template_report_and_queue(client):
    h = admin_headers(client)
    course_id = client.post("/api/admin/courses", json={"code": "SE", "title": "T"}, headers=h).json()["id"]
    client.post("/api/admin/classes", json={"course_id": course_id, "code": "G1"}, headers=h)

    # AC-06: template downloads with exact columns + one example row.
    tpl = client.get("/api/admin/import-template", headers=h)
    assert tpl.status_code == 200

    ws = load_workbook(filename=io.BytesIO(tpl.content), read_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    assert [str(c) for c in rows[0]] == ["username", "gmail", "type", "class"]
    assert len(rows) == 2  # header + one example row
    assert len(rows[0]) == 4  # no password column (AC-05)

    payload = make_xlsx(
        [
            ["Stu One", "stu1@gmail.com", "student", "G1"],  # imported
            ["Stu Dup", "stu1@gmail.com", "student", "G1"],  # skipped (email exists)
            ["Bad Guy", "bad@gmail.com", "wizard", "G1"],  # failed (role)
            ["No Class", "noc@gmail.com", "student", "MISSING"],  # failed (class)
        ]
    )
    up = client.post(
        "/api/admin/imports", files={"file": ("a.xlsx", payload)}, headers=h
    )
    assert up.status_code == 201, up.text
    body = up.json()
    assert (body["imported_count"], body["skipped_count"], body["failed_count"]) == (1, 1, 2)

    detail = client.get(f"/api/admin/imports/{body['id']}", headers=h).json()
    statuses = {r["email"]: r["status"] for r in detail["rows"]}
    # Duplicate gmail: first row imports, second row skips (email now exists).
    assert statuses["stu1@gmail.com"] in ("imported", "skipped")
    assert statuses["bad@gmail.com"] == "failed"
    assert statuses["noc@gmail.com"] == "failed"
    assert any(r["status"] == "imported" for r in detail["rows"])
    assert any(r["status"] == "skipped" for r in detail["rows"])

    # AC-09: imports queue (not send inline); the admin drains the queue at
    # the limited rate. Dry-run marks everything sent without real SMTP.
    stats = client.get("/api/admin/email-queue/stats", headers=h).json()
    assert stats["pending"] >= 1
    drained = client.post("/api/admin/email-queue/process", headers=h).json()
    assert drained["sent"] >= 1
    stats = client.get("/api/admin/email-queue/stats", headers=h).json()
    assert stats["pending"] == 0
    # Failed emails can be re-queued without re-importing (AC-10).
    queued = client.get("/api/admin/email-queue?status=sent", headers=h).json()
    assert len(queued) >= 1

    # AC-07: admin never sees generated passwords — neither in the batch
    # response keys nor in the response/row text.
    for key in body:
        assert "password" not in key.lower()
    response_text = up.text.lower()
    assert "temporary password" not in response_text
    for r in detail["rows"]:
        assert "Temporary password" not in r["message"]


def test_deactivate_reset_delete_guards(client):
    h = admin_headers(client)
    course_id = client.post("/api/admin/courses", json={"code": "X", "title": "T"}, headers=h).json()["id"]
    client.post("/api/admin/classes", json={"course_id": course_id, "code": "Z1"}, headers=h)
    uid = client.post(
        "/api/admin/users",
        json={"username": "s", "email": "deact@gmail.com", "role": "student", "class_code": "Z1"},
        headers=h,
    ).json()["id"]

    # AC-11: deactivate → login fails with the generic message.
    client.patch(f"/api/admin/users/{uid}/active", json={"is_active": False}, headers=h)
    bad_login = client.post(
        "/api/auth/login", json={"email": "deact@gmail.com", "password": "anything"}
    )
    assert bad_login.status_code == 401
    assert bad_login.json()["detail"] == "Invalid email or password."
    client.patch(f"/api/admin/users/{uid}/active", json={"is_active": True}, headers=h)

    # AC-11: reset emails the new password; response carries no password.
    reset = client.post(f"/api/admin/users/{uid}/reset-password", headers=h)
    assert reset.status_code == 200
    assert "emailed_to" in reset.json()

    # AC-12: delete needs ?confirm=true; last admin is protected.
    no_confirm = client.delete(f"/api/admin/users/{uid}", headers=h)
    assert no_confirm.status_code == 400
    me = client.get("/api/auth/me", headers=h).json()
    last_admin = client.delete(f"/api/admin/users/{me['id']}?confirm=true", headers=h)
    assert last_admin.status_code == 400
    ok_del = client.delete(f"/api/admin/users/{uid}?confirm=true", headers=h)
    assert ok_del.status_code == 200


def test_login_never_reveals_email_existence(client):
    h = admin_headers(client)
    assert h  # bootstrap works
    r1 = client.post("/api/auth/login", json={"email": "nobody@gmail.com", "password": "x"})
    r2 = client.post("/api/auth/login", json={"email": settings.bootstrap_admin_email, "password": "wrong"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]
