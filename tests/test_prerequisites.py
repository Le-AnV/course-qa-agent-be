"""Class prerequisites: admin sets them; only a PASSED result unlocks joining."""

import uuid

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


def make_user(client: TestClient, admin: dict, email: str, role: str) -> str:
    res = client.post(
        "/api/admin/users",
        json={"username": role.title(), "email": email, "role": role},
        headers=admin,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def token_for(db_session: Session, email: str, role: str) -> dict:
    from app.core.security import create_access_token
    from app.models.user import User

    user = db_session.query(User).filter(User.email == email).first()
    assert user is not None
    return {"Authorization": f"Bearer {create_access_token(str(user.id), role)}"}


def make_class(
    client: TestClient,
    admin: dict,
    code: str,
    trainer_id: str,
    prereq_ids: list[str] | None = None,
):
    return client.post(
        "/api/admin/classes",
        json={
            "code": code,
            "trainer_id": trainer_id,
            "credits": 3,
            "semester": "S1",
            "academic_year": "2026",
            "prerequisite_class_ids": prereq_ids or [],
        },
        headers=admin,
    )


def test_admin_sets_prerequisites_on_create_and_can_replace_them(api_client):
    h = admin_headers(api_client)
    trainer = make_user(api_client, h, "pre1-t@gmail.com", "trainer")
    a = make_class(api_client, h, "PRE-A", trainer)
    assert a.status_code == 201, a.text
    b = make_class(api_client, h, "PRE-B", trainer, prereq_ids=[a.json()["id"]])
    assert b.status_code == 201, b.text
    assert [p["code"] for p in b.json()["prerequisites"]] == ["PRE-A"]

    listed = api_client.get("/api/admin/classes", headers=h).json()
    row = next(c for c in listed if c["id"] == b.json()["id"])
    assert [p["id"] for p in row["prerequisites"]] == [a.json()["id"]]

    cleared = api_client.patch(
        f"/api/admin/classes/{b.json()['id']}/prerequisites",
        json={"prerequisite_class_ids": []},
        headers=h,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["prerequisites"] == []

    # Self-reference and unknown ids are rejected.
    self_ref = api_client.patch(
        f"/api/admin/classes/{b.json()['id']}/prerequisites",
        json={"prerequisite_class_ids": [b.json()["id"]]},
        headers=h,
    )
    assert self_ref.status_code == 400, self_ref.text
    unknown = api_client.patch(
        f"/api/admin/classes/{b.json()['id']}/prerequisites",
        json={"prerequisite_class_ids": [str(uuid.uuid4())]},
        headers=h,
    )
    assert unknown.status_code == 400, unknown.text


def test_catalog_blocks_request_until_prerequisite_is_passed(api_client, db_session):
    h = admin_headers(api_client)
    trainer = make_user(api_client, h, "pre2-t@gmail.com", "trainer")
    student = make_user(api_client, h, "pre2-s@gmail.com", "student")
    sh = token_for(db_session, "pre2-s@gmail.com", "student")
    a = make_class(api_client, h, "PRE2-A", trainer).json()["id"]
    b = make_class(api_client, h, "PRE2-B", trainer, prereq_ids=[a]).json()["id"]

    row = next(c for c in api_client.get("/api/student/catalog", headers=sh).json() if c["id"] == b)
    assert row["can_request"] is False
    assert [p["code"] for p in row["blocked_prerequisites"]] == ["PRE2-A"]

    blocked = api_client.post(f"/api/student/classes/{b}/request", headers=sh)
    assert blocked.status_code == 400, blocked.text
    assert "PRE2-A" in blocked.json()["detail"]

    # Approving a stale pending row is gated too (no trusting the client).
    db_session.add(
        Enrollment(class_id=uuid.UUID(b), student_id=uuid.UUID(student), status="PENDING")
    )
    db_session.commit()
    approve = api_client.post(
        f"/api/student/classes/{b}/requests/{student}/approve", headers=h
    )
    assert approve.status_code == 400, approve.text
    db_session.query(Enrollment).filter(Enrollment.student_id == uuid.UUID(student)).delete()
    db_session.commit()

    # Marking PASSED on the prerequisite unlocks it.
    passed = api_client.patch(
        f"/api/student/classes/{a}/members/{student}/result",
        json={"result": "PASSED"},
        headers=token_for(db_session, "pre2-t@gmail.com", "trainer"),
    )
    assert passed.status_code == 400, passed.text  # not enrolled in A yet

    api_client.post(f"/api/student/classes/{a}/request", headers=sh)
    assert (
        api_client.post(f"/api/student/classes/{a}/requests/{student}/approve", headers=h).status_code
        == 200
    )
    passed = api_client.patch(
        f"/api/student/classes/{a}/members/{student}/result",
        json={"result": "PASSED"},
        headers=token_for(db_session, "pre2-t@gmail.com", "trainer"),
    )
    assert passed.status_code == 200, passed.text
    assert passed.json()["result"] == "PASSED"

    row = next(c for c in api_client.get("/api/student/catalog", headers=sh).json() if c["id"] == b)
    assert row["can_request"] is True
    assert row["blocked_prerequisites"] == []

    assert api_client.post(f"/api/student/classes/{b}/request", headers=sh).status_code == 200
    assert (
        api_client.post(f"/api/student/classes/{b}/requests/{student}/approve", headers=h).status_code
        == 200
    )


def test_only_the_class_trainer_can_record_results(api_client, db_session):
    h = admin_headers(api_client)
    trainer = make_user(api_client, h, "pre3-t@gmail.com", "trainer")
    make_user(api_client, h, "pre3-other@gmail.com", "trainer")
    student = make_user(api_client, h, "pre3-s@gmail.com", "student")
    cls = make_class(api_client, h, "PRE3-A", trainer).json()["id"]

    sh = token_for(db_session, "pre3-s@gmail.com", "student")
    api_client.post(f"/api/student/classes/{cls}/request", headers=sh)
    api_client.post(f"/api/student/classes/{cls}/requests/{student}/approve", headers=h)

    payload = {"result": "FAILED"}
    assert (
        api_client.patch(
            f"/api/student/classes/{cls}/members/{student}/result", json=payload, headers=h
        ).status_code
        == 403
    )
    assert (
        api_client.patch(
            f"/api/student/classes/{cls}/members/{student}/result",
            json=payload,
            headers=token_for(db_session, "pre3-other@gmail.com", "trainer"),
        ).status_code
        == 403
    )
    own = api_client.patch(
        f"/api/student/classes/{cls}/members/{student}/result",
        json=payload,
        headers=token_for(db_session, "pre3-t@gmail.com", "trainer"),
    )
    assert own.status_code == 200, own.text
    assert own.json()["result"] == "FAILED"
