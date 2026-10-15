"""Credit rules: manual class credits (1-6), hard 20-credit cap per semester."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.services import course_service

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


def make_trainer(client, h, email):
    res = client.post(
        "/api/admin/users",
        json={"username": "T", "email": email, "role": "trainer"},
        headers=h,
    )
    assert res.status_code == 201, res.text
    return res.json()


def make_class(client, h, code, trainer_id, credits=3, semester="S1", year="2026"):
    return client.post(
        "/api/admin/classes",
        json={
            "code": code,
            "trainer_id": trainer_id,
            "credits": credits,
            "semester": semester,
            "academic_year": year,
        },
        headers=h,
    )


def test_class_credits_validation(api_client):
    h = admin_headers(api_client)
    trainer = make_trainer(api_client, h, "cr-t@gmail.com")

    bad = make_class(api_client, h, "CR1-A", trainer["id"], credits=0)
    assert bad.status_code in (400, 422), bad.text
    bad = make_class(api_client, h, "CR1-A", trainer["id"], credits=7)
    assert bad.status_code in (400, 422), bad.text

    ok = make_class(api_client, h, "CR1-A", trainer["id"], credits=5)
    assert ok.status_code == 201, ok.text
    assert ok.json()["credits"] == 5

    defaulted = make_class(api_client, h, "CR1-B", trainer["id"])
    # Pydantic default when omitted
    assert defaulted.status_code == 201, defaulted.text
    assert defaulted.json()["credits"] == 3


def test_semester_cap_blocks_fourth_approval(api_client, db_session):
    h = admin_headers(api_client)
    trainer = make_trainer(api_client, h, "cr2-t@gmail.com")
    tids = trainer["id"]

    # 4 standalone classes x 6 credits in the same term.
    cids = []
    for i in range(4):
        res = make_class(api_client, h, f"CL2-{i}", tids, credits=6,
                         semester="S9", year="2099")
        assert res.status_code == 201, res.text
        cids.append(res.json()["id"])

    # One student requests all four; approve three (18 credits) then the 4th must fail.
    stu = api_client.post(
        "/api/admin/users",
        json={"username": "S", "email": "cap@gmail.com", "role": "student"},
        headers=h,
    )
    assert stu.status_code == 201, stu.text
    student_id = stu.json()["id"]

    from app.core.security import create_access_token

    student_h = {"Authorization": f"Bearer {create_access_token(str(student_id), 'student')}"}

    for cid in cids[:3]:
        req = api_client.post(f"/api/student/classes/{cid}/request", headers=student_h)
        assert req.status_code == 200, req.text
        ok = api_client.post(
            f"/api/student/classes/{cid}/requests/{student_id}/approve", headers=h
        )
        assert ok.status_code == 200, ok.text

    total = course_service.semester_credit_total(db_session, student_id, "S9", "2099")
    assert total == 18

    req = api_client.post(f"/api/student/classes/{cids[3]}/request", headers=student_h)
    assert req.status_code == 200, req.text
    blocked = api_client.post(
        f"/api/student/classes/{cids[3]}/requests/{student_id}/approve", headers=h
    )
    assert blocked.status_code == 400, blocked.text
    assert "20 credits" in blocked.json()["detail"]
