"""DB login coverage (AI-25) on the ERD UUID model.

Uses in-memory SQLite (StaticPool) so CI stays DB-free. Covers:
- seed roles/users (UUID User/Account),
- verify_credentials helper (DB-backed credential check),
- POST /api/auth/login (UUID LoginResponse, generic 401),
- GET /api/auth/me with class scope.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.seed import seed_database
from app.main import app
from app.models.user import Role, User
from app.services.auth import verify_credentials

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


def test_seed_creates_roles_and_uuid_users(db_session):
    seed_database(db_session)

    role_names = {r.name for r in db_session.query(Role).all()}
    assert {"ADMIN", "TRAINER", "STUDENT"} <= role_names

    admin = db_session.query(User).filter(User.email == "admin@academic.edu.vn").first()
    assert admin is not None
    assert isinstance(admin.id, uuid.UUID)
    assert admin.role.name == "ADMIN"
    assert admin.full_name


def test_verify_credentials_db_backed(db_session):
    seed_database(db_session)

    result = verify_credentials("admin@academic.edu.vn", "Admin@123", db=db_session)
    assert result["identifier"] == "admin@academic.edu.vn"
    assert result["role"] == "admin"

    result = verify_credentials("teacher@academic.edu.vn", "Teacher@123", db=db_session)
    assert result["role"] == "trainer"

    result = verify_credentials("student@academic.edu.vn", "Student@123", db=db_session)
    assert result["role"] == "student"

    with pytest.raises(Exception) as exc_info:
        verify_credentials("admin@academic.edu.vn", "WrongPassword", db=db_session)
    assert getattr(exc_info.value, "status_code", None) == 401


def test_api_login_uuid_contract(client):
    db_session = client  # noqa: F841 (fixture provides seeded DB via lifespan below)
    # Seed through a direct session so passwords are known.
    from app.core.database import get_session_local as _gsl

    SessionLocal = _gsl(TEST_DB_URL)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()

    # Admin has no class scope — class_id is None.
    res = client.post(
        "/api/auth/login",
        json={"email": "admin@academic.edu.vn", "password": "Admin@123"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["role"] == "admin"
    assert body["username"] == "System Administrator"
    uuid.UUID(str(body["user_id"]))
    assert body["class_id"] is None
    assert body["access_token"]

    # Student (no class gate for STUDENT) can log in without enrollment.
    res = client.post(
        "/api/auth/login",
        json={"email": "student@academic.edu.vn", "password": "Student@123"},
    )
    assert res.status_code == 200, res.text

    # Seeded teacher owns CS101-FA26-001, so they can log in with class scope.
    res = client.post(
        "/api/auth/login",
        json={"email": "teacher@academic.edu.vn", "password": "Teacher@123"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["class_id"] is not None

    # A trainer without an assigned class is still blocked by the login gate (AC-03).
    from app.core.database import get_session_local as _gsl2
    from app.core.security import hash_password
    from app.models.user import Account, Role, User

    SessionLocal2 = _gsl2(TEST_DB_URL)
    db2 = SessionLocal2()
    try:
        role = db2.query(Role).filter(Role.name == "TRAINER").first()
        user = User(
            full_name="Unassigned Trainer",
            email="unassigned-trainer@academic.edu.vn",
            role_id=role.id,
            status="ACTIVE",
        )
        db2.add(user)
        db2.flush()
        db2.add(
            Account(
                user_id=user.id,
                password_hash=hash_password("Unassigned@123"),
                is_active=True,
            )
        )
        db2.commit()
    finally:
        db2.close()

    res = client.post(
        "/api/auth/login",
        json={"email": "unassigned-trainer@academic.edu.vn", "password": "Unassigned@123"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password."

    # Wrong password and unknown email share the same generic message.
    bad = client.post(
        "/api/auth/login",
        json={"email": "admin@academic.edu.vn", "password": "WrongPassword"},
    )
    nobody = client.post(
        "/api/auth/login", json={"email": "nobody@gmail.com", "password": "x"}
    )
    assert bad.status_code == nobody.status_code == 401
    assert bad.json()["detail"] == nobody.json()["detail"]


def test_me_returns_uuid_scope(client):
    from app.core.database import get_session_local as _gsl

    SessionLocal = _gsl(TEST_DB_URL)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()

    login = client.post(
        "/api/auth/login",
        json={"email": "admin@academic.edu.vn", "password": "Admin@123"},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    body = me.json()
    uuid.UUID(str(body["id"]))
    assert body["role"] == "admin"
    assert body["is_active"] is True
