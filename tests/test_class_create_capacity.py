"""Creating a class with a roster capacity in the same request."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.models.course import ClassConfig

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


def make_trainer(client: TestClient, admin: dict) -> str:
    res = client.post(
        "/api/admin/users",
        json={
            "username": "capacity_trainer",
            "email": "capacity_trainer@example.com",
            "role": "trainer",
        },
        headers=admin,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_create_class_with_capacity_returns_and_persists_it(api_client, db_session):
    admin = admin_headers(api_client)
    trainer_id = make_trainer(api_client, admin)

    res = api_client.post(
        "/api/admin/classes",
        json={
            "code": "CS101-FA26-003",
            "trainer_id": trainer_id,
            "credits": 3,
            "capacity": 40,
        },
        headers=admin,
    )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["capacity"] == 40

    row = (
        db_session.query(ClassConfig)
        .filter(ClassConfig.class_id == uuid.UUID(body["id"]))
        .one()
    )
    assert row.capacity == 40

@pytest.mark.parametrize("capacity", [0, 501])
def test_create_class_rejects_capacity_out_of_range(api_client, capacity):
    admin = admin_headers(api_client)
    trainer_id = make_trainer(api_client, admin)

    res = api_client.post(
        "/api/admin/classes",
        json={
            "code": f"CS101-FA26-{capacity}",
            "trainer_id": trainer_id,
            "capacity": capacity,
        },
        headers=admin,
    )

    assert res.status_code == 422, res.text
