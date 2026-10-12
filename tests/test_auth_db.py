"""Integration and unit tests for database connection and authentication."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.database import SessionLocal, check_db_connection
from app.core.seed import seed_database
from app.main import app
from app.models.user import Role, User
from app.services.auth import verify_credentials


def test_database_connection():
    """Verify that PostgreSQL database connection is alive."""
    assert check_db_connection() is True


def test_seed_database_and_verify_credentials():
    """Verify that seeding creates the expected roles and user accounts."""
    with SessionLocal() as db:
        seed_database(db)

        # Check roles
        roles = db.query(Role).all()
        role_names = {r.name for r in roles}
        assert "ADMIN" in role_names
        assert "TRAINER" in role_names
        assert "STUDENT" in role_names

        # Check admin user
        admin_user = db.query(User).filter(User.email == "admin@academic.edu.vn").first()
        assert admin_user is not None
        assert admin_user.role.name == "ADMIN"

        # Check admin authentication with DB session
        result = verify_credentials("admin@academic.edu.vn", "Admin@123", db=db)
        assert result["identifier"] == "admin@academic.edu.vn"
        assert result["role"] == "admin"

        # Check trainer authentication with DB session
        result = verify_credentials("teacher@academic.edu.vn", "Teacher@123", db=db)
        assert result["identifier"] == "teacher@academic.edu.vn"
        assert result["role"] == "trainer"

        # Check student authentication with DB session
        result = verify_credentials("student@academic.edu.vn", "Student@123", db=db)
        assert result["identifier"] == "student@academic.edu.vn"
        assert result["role"] == "student"

        # Check invalid password raises 401
        with pytest.raises(HTTPException) as exc_info:
            verify_credentials("admin@academic.edu.vn", "WrongPassword", db=db)
        assert exc_info.value.status_code == 401


def test_auth_login_api_endpoint():
    """Test the POST /auth/login endpoint using the real DB."""
    client = TestClient(app)

    response = client.post(
        "/auth/login",
        json={"identifier": "admin@academic.edu.vn", "password": "Admin@123"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["role"] == "admin"

    response = client.post(
        "/auth/login",
        json={"identifier": "admin@academic.edu.vn", "password": "WrongPassword"},
    )
    assert response.status_code == 401
