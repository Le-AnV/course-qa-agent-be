"""HTTP tests for student workspace endpoints (enrolled classes, members, materials)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
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


def login(client: TestClient, email: str, password: str) -> dict:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def classroom(api_client):
    """Course + trainer-owned class + one enrolled student. Returns headers + ids."""
    admin = login(api_client, settings.bootstrap_admin_email, settings.bootstrap_admin_password)
    course = api_client.post(
        "/api/admin/courses", json={"code": "STU", "title": "Student WS"}, headers=admin
    ).json()
    trainer = api_client.post(
        "/api/admin/users",
        json={"username": "T", "email": "t-ws@gmail.com", "role": "trainer"},
        headers=admin,
    )
    assert trainer.status_code == 201, trainer.text
    klass = api_client.post(
        "/api/admin/classes",
        json={
            "course_id": course["id"],
            "code": "STU-A",
            "trainer_id": trainer.json()["id"],
            "name": "STU A",
        },
        headers=admin,
    )
    assert klass.status_code == 201, klass.text
    student = api_client.post(
        "/api/admin/users",
        json={"username": "S", "email": "s-ws@gmail.com", "role": "student", "class_code": "STU-A"},
        headers=admin,
    )
    assert student.status_code == 201, student.text
    return {"admin": admin, "class_id": klass.json()["id"]}


def student_headers(api_client, email="s-ws@gmail.com", role="student"):
    from app.core.database import get_session_local
    from app.core.security import create_access_token
    from app.models.user import User

    db = get_session_local(settings.database_url)()
    try:
        user = db.query(User).filter(User.email == email).first()
        return {"Authorization": f"Bearer {create_access_token(str(user.id), role)}"}
    finally:
        db.close()


def test_student_sees_enrolled_class(api_client, classroom):
    res = api_client.get("/api/student/classes", headers=student_headers(api_client))
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["code"] == "STU-A"
    assert body[0]["trainer_name"] == "T"
    assert body[0]["member_count"] == 2  # trainer + student


def test_student_class_detail_members_documents(api_client, classroom):
    h = student_headers(api_client)
    cid = classroom["class_id"]
    detail = api_client.get(f"/api/student/classes/{cid}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["code"] == "STU-A"

    members = api_client.get(f"/api/student/classes/{cid}/members", headers=h)
    assert members.status_code == 200
    roles = sorted(m["role"] for m in members.json())
    assert roles == ["student", "trainer"]

    docs = api_client.get(f"/api/student/classes/{cid}/documents", headers=h)
    assert docs.status_code == 200
    assert docs.json() == []


def test_non_member_is_forbidden(api_client, classroom):
    outsider = api_client.post(
        "/api/admin/users",
        json={"username": "Out", "email": "out-ws@gmail.com", "role": "student"},
        headers=classroom["admin"],
    )
    assert outsider.status_code == 201
    h = student_headers(api_client, email="out-ws@gmail.com")
    cid = classroom["class_id"]
    assert api_client.get(f"/api/student/classes/{cid}", headers=h).status_code == 403
    assert api_client.get("/api/student/classes", headers=h).json() == []
    assert (
        api_client.get(
            "/api/student/classes/00000000-0000-0000-0000-000000000000", headers=h
        ).status_code
        == 404
    )


def test_join_request_and_ta_approval(api_client, classroom):
    admin, cid = classroom["admin"], classroom["class_id"]
    # Outsider browses the catalog: class listed with no relation.
    out = api_client.post(
        "/api/admin/users",
        json={"username": "Out", "email": "out2-ws@gmail.com", "role": "student"},
        headers=admin,
    )
    assert out.status_code == 201
    outsider = student_headers(api_client, email="out2-ws@gmail.com")
    catalog = api_client.get("/api/student/catalog", headers=outsider).json()
    mine = next(c for c in catalog if c["id"] == cid)
    assert mine["my_status"] == "none"

    # Request to join -> pending.
    req = api_client.post(f"/api/student/classes/{cid}/request", headers=outsider)
    assert req.status_code == 200, req.text
    assert req.json()["status"] == "pending"
    # Double request is rejected.
    again = api_client.post(f"/api/student/classes/{cid}/request", headers=outsider)
    assert again.status_code == 400
    # Pending request is NOT membership yet.
    assert api_client.get(f"/api/student/classes/{cid}", headers=outsider).status_code == 403

    # A TA enrolled in the class can review + approve.
    ta = api_client.post(
        "/api/admin/users",
        json={
            "username": "TA",
            "email": "ta-ws@gmail.com",
            "role": "teaching_assistant",
            "class_code": "STU-A",
        },
        headers=admin,
    )
    assert ta.status_code == 201, ta.text
    ta_h = student_headers(api_client, email="ta-ws@gmail.com", role="teaching_assistant")
    pending = api_client.get(f"/api/student/classes/{cid}/requests", headers=ta_h)
    assert pending.status_code == 200, pending.text
    assert len(pending.json()) == 1

    # The requester themselves cannot approve.
    forbidden = api_client.post(
        f"/api/student/classes/{cid}/requests/{out.json()['id']}/approve", headers=outsider
    )
    assert forbidden.status_code == 403

    approved = api_client.post(
        f"/api/student/classes/{cid}/requests/{out.json()['id']}/approve", headers=ta_h
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "active"
    # Now a real member.
    detail = api_client.get(f"/api/student/classes/{cid}", headers=outsider)
    assert detail.status_code == 200
    members = api_client.get(f"/api/student/classes/{cid}/members", headers=outsider).json()
    assert "out2-ws@gmail.com" in [m["email"] for m in members]


def test_reject_removes_request(api_client, classroom):
    cid = classroom["class_id"]
    out = api_client.post(
        "/api/admin/users",
        json={"username": "Out3", "email": "out3-ws@gmail.com", "role": "student"},
        headers=classroom["admin"],
    )
    assert out.status_code == 201
    outsider = student_headers(api_client, email="out3-ws@gmail.com")
    req = api_client.post(f"/api/student/classes/{cid}/request", headers=outsider)
    assert req.status_code == 200
    # Admin rejects.
    rej = api_client.post(
        f"/api/student/classes/{cid}/requests/{out.json()['id']}/reject",
        headers=classroom["admin"],
    )
    assert rej.status_code == 200
    assert api_client.get("/api/student/requests", headers=outsider).json() == []
