"""Multiple trainers per class (co-teaching / mid-semester handover).

Covers the admin API contract:
- POST   /api/admin/classes            -> ClassRead.trainers lists the primary
- POST   /api/admin/classes/{id}/trainers            -> add a co-trainer
- DELETE /api/admin/classes/{id}/trainers/{tid}      -> remove a co-trainer
- a co-trainer sees the class in /api/student/classes and /api/documents/classes/mine
"""

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


def make_trainer(client: TestClient, admin: dict, email: str, name: str) -> str:
    res = client.post(
        "/api/admin/users",
        json={"username": name, "email": email, "role": "trainer"},
        headers=admin,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def login_token(client: TestClient, db_session, email: str) -> dict:
    from app.core.security import create_access_token
    from app.models.user import User

    user = db_session.query(User).filter(User.email == email).first()
    return {"Authorization": f"Bearer {create_access_token(str(user.id), 'trainer')}"}


def test_create_class_exposes_the_primary_trainer(api_client):
    admin = admin_headers(api_client)
    trainer = make_trainer(api_client, admin, "t1@gmail.com", "Trainer One")

    res = api_client.post(
        "/api/admin/classes",
        json={"code": "CS101-A", "trainer_id": trainer, "credits": 3},
        headers=admin,
    )
    assert res.status_code == 201, res.text
    trainers = res.json()["trainers"]
    assert len(trainers) == 1
    assert trainers[0]["trainer_id"] == trainer
    assert trainers[0]["is_primary"] is True


def test_add_and_remove_a_co_trainer(api_client):
    admin = admin_headers(api_client)
    first = make_trainer(api_client, admin, "t1@gmail.com", "Trainer One")
    second = make_trainer(api_client, admin, "t2@gmail.com", "Trainer Two")
    class_id = api_client.post(
        "/api/admin/classes",
        json={"code": "CS101-A", "trainer_id": first, "credits": 3},
        headers=admin,
    ).json()["id"]

    added = api_client.post(
        f"/api/admin/classes/{class_id}/trainers",
        json={"trainer_id": second},
        headers=admin,
    )
    assert added.status_code == 200, added.text
    trainers = added.json()["trainers"]
    assert [t["trainer_id"] for t in trainers] == [first, second]
    assert [t["is_primary"] for t in trainers] == [True, False]

    # Adding the same trainer twice is rejected, not silently duplicated.
    again = api_client.post(
        f"/api/admin/classes/{class_id}/trainers",
        json={"trainer_id": second},
        headers=admin,
    )
    assert again.status_code == 400

    removed = api_client.delete(
        f"/api/admin/classes/{class_id}/trainers/{second}", headers=admin
    )
    assert removed.status_code == 200, removed.text
    assert [t["trainer_id"] for t in removed.json()["trainers"]] == [first]


def test_primary_trainer_cannot_be_removed_through_the_co_trainer_route(api_client):
    admin = admin_headers(api_client)
    first = make_trainer(api_client, admin, "t1@gmail.com", "Trainer One")
    class_id = api_client.post(
        "/api/admin/classes",
        json={"code": "CS101-A", "trainer_id": first, "credits": 3},
        headers=admin,
    ).json()["id"]

    res = api_client.delete(f"/api/admin/classes/{class_id}/trainers/{first}", headers=admin)
    assert res.status_code == 400
    assert "primary" in res.json()["detail"].lower()


def test_co_trainer_sees_the_class_in_their_workspace(api_client, db_session):
    admin = admin_headers(api_client)
    first = make_trainer(api_client, admin, "t1@gmail.com", "Trainer One")
    second = make_trainer(api_client, admin, "t2@gmail.com", "Trainer Two")
    class_id = api_client.post(
        "/api/admin/classes",
        json={"code": "CS101-A", "trainer_id": first, "credits": 3},
        headers=admin,
    ).json()["id"]

    second_auth = login_token(api_client, db_session, "t2@gmail.com")
    before = api_client.get("/api/student/classes", headers=second_auth).json()
    assert before == []

    api_client.post(
        f"/api/admin/classes/{class_id}/trainers",
        json={"trainer_id": second},
        headers=admin,
    )

    after = api_client.get("/api/student/classes", headers=second_auth).json()
    assert [c["id"] for c in after] == [class_id]

    details = api_client.get(f"/api/student/classes/{class_id}", headers=second_auth)
    assert details.status_code == 200, details.text
    assert details.json()["trainer_name"] == "Trainer One"

    members = api_client.get(
        f"/api/student/classes/{class_id}/members", headers=second_auth
    ).json()
    trainer_names = sorted(m["username"] for m in members if m["role"] == "trainer")
    assert trainer_names == ["Trainer One", "Trainer Two"]

    mine = api_client.get("/api/documents/classes/mine", headers=second_auth).json()
    assert [c["id"] for c in mine] == [class_id]
