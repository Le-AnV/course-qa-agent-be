"""Tests for Epic E2: upload / list / tag documents (AI-35/37/41).

Auth (AI-25/27): endpoints require `Authorization: Bearer <JWT>` for a
DB-backed login token (trainer+admin allowed, student denied, missing
token = 401). Uses in-memory SQLite (StaticPool) so CI stays DB-free.
"""

import io
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.security import hash_password
from app.core.seed import seed_database
from app.main import app
from app.models.course import Class
from app.models.user import Account, Role, User
from app.services import document_service

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
        seed_database(db)
        _seed_classes(db)
        yield db
    finally:
        db.close()
        engine.dispose()
    reset_engine()


def _make_user(db: Session, email: str, full_name: str, password: str, role_name: str) -> User:
    role = db.query(Role).filter(Role.name == role_name).first()
    user = User(full_name=full_name, email=email, role_id=role.id, status="ACTIVE")
    db.add(user)
    db.flush()
    db.add(Account(user_id=user.id, password_hash=hash_password(password), is_active=True))
    db.flush()
    return user


def _seed_classes(db: Session) -> None:
    """Standalone classes (no course binding): class-a + class-b owned by the
    seed teacher, class-c by another trainer. Class codes are kept stable for
    the document scope tests below.
    """
    teacher = db.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    other = _make_user(db, "other-teacher@academic.edu.vn", "Other Trainer", "Other@123", "TRAINER")
    specs = (
        ("CS101-A", "CS101 - Class A", teacher),
        ("CS101-B", "CS101 - Class B", teacher),
        ("CS101-C", "CS101 - Class C", other),
    )
    for class_code, class_name, owner in specs:
        db.add(
            Class(
                trainer_id=owner.id,
                code=class_code,
                name=class_name,
                semester="HK1",
                academic_year="2026-2027",
                status="ACTIVE",
            )
        )
    db.commit()


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


def _login_headers(client: TestClient, email: str, password: str) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def trainer_headers(client):
    return _login_headers(client, "teacher@academic.edu.vn", "Teacher@123")


@pytest.fixture()
def admin_headers(client):
    return _login_headers(client, "admin@academic.edu.vn", "Admin@123")


@pytest.fixture()
def student_headers(client):
    return _login_headers(client, "student@academic.edu.vn", "Student@123")


@pytest.fixture()
def other_trainer_headers(client):
    return _login_headers(client, "other-teacher@academic.edu.vn", "Other@123")


def _class_id(client, headers, code: str) -> str:
    rows = client.get("/api/documents/classes/mine", headers=headers).json()
    return next(c["id"] for c in rows if c["code"] == code)


def _zip_bytes(inner: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<types/>")
        zf.writestr(inner, "x")
    return buf.getvalue()


def test_accepted_types_lists_seven_material_formats(client, trainer_headers):
    response = client.get("/api/documents/accepted-types", headers=trainer_headers)
    assert response.status_code == 200
    extensions = {t["extension"] for t in response.json()}
    assert extensions == {".pdf", ".docx", ".md", ".txt", ".pptx", ".xlsx", ".csv"}


def test_accepted_types_requires_auth(client, student_headers):
    assert client.get("/api/documents/accepted-types").status_code in (401, 403)
    assert client.get("/api/documents/accepted-types", headers=student_headers).status_code == 403


def test_upload_pdf_is_pending_approval(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    files = [("files", ("syllabus.pdf", b"%PDF-1.4 content", "application/pdf"))]
    response = client.post(
        "/api/documents/", files=files, data={"class_id": class_a}, headers=trainer_headers
    )
    assert response.status_code == 201
    result = response.json()["uploaded"][0]
    assert result["error"] is None
    assert result["document"]["status"] == "pending"
    assert result["document"]["class_id"] == class_a


def test_multi_file_upload_reports_per_file_errors(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    files = [
        ("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf")),
        ("files", ("virus.exe", b"MZ binary", "application/octet-stream")),
        ("files", ("notes.txt", b"hello", "text/plain")),
    ]
    response = client.post(
        "/api/documents/", files=files, data={"class_id": class_a}, headers=trainer_headers
    )
    assert response.status_code == 201
    uploaded = response.json()["uploaded"]
    assert uploaded[0]["error"] is None
    assert "Invalid file format" in uploaded[1]["error"]
    assert uploaded[2]["error"] is None


def test_renamed_exe_to_pdf_is_rejected(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    files = [("files", ("evil.pdf", b"MZ\x90\x00 binary", "application/pdf"))]
    response = client.post(
        "/api/documents/", files=files, data={"class_id": class_a}, headers=trainer_headers
    )
    assert response.status_code == 201
    assert "not a real PDF" in response.json()["uploaded"][0]["error"]


def test_renamed_txt_to_docx_is_rejected(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    files = [("files", ("fake.docx", b"plain text, not zip", "application/vnd.openxmlformats"))]
    response = client.post(
        "/api/documents/", files=files, data={"class_id": class_a}, headers=trainer_headers
    )
    assert response.status_code == 201
    assert "not a real .docx" in response.json()["uploaded"][0]["error"]


def test_oversize_upload_is_rejected(db_session, client, trainer_headers):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = _class_id(client, trainer_headers, "CS101-A")
    old = document_service.MAX_FILE_SIZE_BYTES
    document_service.MAX_FILE_SIZE_BYTES = 10
    try:
        error = document_service.validate_upload(
            db_session, "big.pdf", b"%PDF-" + b"x" * 20, class_a, teacher
        )
    finally:
        document_service.MAX_FILE_SIZE_BYTES = old
    assert isinstance(error, str) and "100MB" in error


def test_classes_mine_only_returns_assigned(
    client, trainer_headers, admin_headers, student_headers
):
    mine = {
        c["code"] for c in client.get("/api/documents/classes/mine", headers=trainer_headers).json()
    }
    # Own classes are returned (seed may add more, e.g. CS101-FA26-001),
    # but another trainer's class never is.
    assert {"CS101-A", "CS101-B"} <= mine
    assert "CS101-C" not in mine
    # Admin sees every class.
    seen = {
        c["code"] for c in client.get("/api/documents/classes/mine", headers=admin_headers).json()
    }
    assert {"CS101-A", "CS101-B", "CS101-C"} <= seen
    assert client.get("/api/documents/classes/mine", headers=student_headers).status_code == 403


def test_list_search_filter_pagination_and_scope(
    db_session, client, trainer_headers, other_trainer_headers, student_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    other = db_session.query(User).filter(User.email == "other-teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    class_b = db_session.query(Class).filter(Class.code == "CS101-B").first()
    document_service.create_document(db_session, "algebra.pdf", b"%PDF-a", class_a.id, teacher)
    document_service.create_document(db_session, "algebra-notes.txt", b"notes", class_a.id, teacher)
    document_service.create_document(db_session, "physics.pdf", b"%PDF-p", class_b.id, teacher)

    # Service-level scope: single-class trainer only sees class-c (List AC-05).
    _, total = document_service.list_documents(db_session, other)
    assert total == 0

    search = client.get(
        "/api/documents/", params={"search": "algebra"}, headers=trainer_headers
    ).json()
    assert search["total"] == 2

    filtered = client.get(
        "/api/documents/",
        params={"file_type": ".pdf", "status": "pending"},
        headers=trainer_headers,
    ).json()
    assert filtered["total"] == 2
    assert all(i["status"] == "pending" for i in filtered["items"])

    page1 = client.get(
        "/api/documents/", params={"page": 1, "page_size": 2}, headers=trainer_headers
    ).json()
    page2 = client.get(
        "/api/documents/", params={"page": 2, "page_size": 2}, headers=trainer_headers
    ).json()
    assert page1["total"] == 3 and len(page1["items"]) == 2 and len(page2["items"]) == 1

    row = page1["items"][0]
    assert {"file_name", "file_type", "size_bytes", "uploader_id", "class_id", "created_at"} <= set(
        row
    )

    assert client.get("/api/documents/", headers=student_headers).status_code == 403


def test_upload_to_unassigned_class_is_rejected(
    db_session, client, trainer_headers, other_trainer_headers, student_headers
):
    other = db_session.query(User).filter(User.email == "other-teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    # Service-level scope check still enforced.
    error = document_service.validate_upload(db_session, "a.pdf", b"%PDF-1.4 a", class_a.id, other)
    assert isinstance(error, str) and "not assigned" in error
    # API-level: per-file error (201), student blocked by RBAC.
    files = [("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf"))]
    denied = client.post(
        "/api/documents/",
        files=files,
        data={"class_id": str(class_a.id)},
        headers=other_trainer_headers,
    )
    assert denied.status_code == 201
    assert "not assigned" in denied.json()["uploaded"][0]["error"]
    forbidden = client.post(
        "/api/documents/", files=files, data={"class_id": str(class_a.id)}, headers=student_headers
    )
    assert forbidden.status_code == 403


def test_approve_trainer_only(db_session, client, trainer_headers, student_headers):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    forbidden = client.post(f"/api/documents/{record.id}/approve", headers=student_headers)
    assert forbidden.status_code == 403
    response = client.post(f"/api/documents/{record.id}/approve", headers=trainer_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "processing"


def test_approve_other_class_is_forbidden(
    db_session, client, trainer_headers, other_trainer_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    response = client.post(f"/api/documents/{record.id}/approve", headers=other_trainer_headers)
    assert response.status_code == 403


def test_qdrant_payload_scopes_by_class(db_session, client, trainer_headers):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    payload = document_service.build_qdrant_payload(record)
    assert payload["class_id"] == str(class_a.id)
    assert payload["document_id"] == str(record.id)
    assert uuid.UUID(payload["document_id"])
