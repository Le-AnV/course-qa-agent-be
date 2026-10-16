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
from app.models.course import Class, Course, Enrollment
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
    """class-a + class-b (seed teacher) and class-c (another trainer) share
    course CS101; a third trainer owns a class in course PHY101.

    Reuses the seeded CS101 course — inserting a second CS101 violates the
    unique code constraint.
    """
    course = db.query(Course).filter(Course.code == "CS101").first()
    assert course is not None
    teacher = db.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    other = _make_user(db, "other-teacher@academic.edu.vn", "Other Trainer", "Other@123", "TRAINER")
    for code, name in (("CS101-A", "CS101 - Class A"), ("CS101-B", "CS101 - Class B")):
        db.add(
            Class(
                course_id=course.id,
                trainer_id=teacher.id,
                code=code,
                name=name,
                semester="HK1",
                academic_year="2026-2027",
                status="ACTIVE",
            )
        )
    db.add(
        Class(
            course_id=course.id,
            trainer_id=other.id,
            code="CS101-C",
            name="CS101 - Class C",
            semester="HK1",
            academic_year="2026-2027",
            status="ACTIVE",
        )
    )
    # A trainer in a *different* course, for D6 (out-of-course => 404).
    physics = Course(code="PHY101", name="Physics", description="d", status="ACTIVE")
    db.add(physics)
    db.flush()
    outsider = _make_user(
        db, "outsider@academic.edu.vn", "Outside Trainer", "Outside@123", "TRAINER"
    )
    db.add(
        Class(
            course_id=physics.id,
            trainer_id=outsider.id,
            code="PHY101-C",
            name="PHY101 - Class C",
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


@pytest.fixture()
def outsider_headers(client):
    return _login_headers(client, "outsider@academic.edu.vn", "Outside@123")


def _class_id(client, headers, code: str) -> str:
    rows = client.get("/api/documents/classes/mine", headers=headers).json()
    return next(c["id"] for c in rows if c["code"] == code)


def _course_id(client, headers, code: str) -> str:
    rows = client.get("/api/documents/courses/mine", headers=headers).json()
    return next(c["id"] for c in rows if c["code"] == code)


def _zip_bytes(inner: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<types/>")
        zf.writestr(inner, "x")
    return buf.getvalue()


def test_accepted_types_lists_six_material_formats(client, trainer_headers):
    response = client.get("/api/documents/accepted-types", headers=trainer_headers)
    assert response.status_code == 200
    extensions = {t["extension"] for t in response.json()}
    assert extensions == {".pdf", ".docx", ".md", ".txt", ".pptx", ".xlsx"}


def test_accepted_types_requires_auth(client, student_headers):
    assert client.get("/api/documents/accepted-types").status_code in (401, 403)
    assert client.get("/api/documents/accepted-types", headers=student_headers).status_code == 403


def test_upload_pdf_is_pending_approval(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    files = [("files", ("syllabus.pdf", b"%PDF-1.4 content", "application/pdf"))]
    response = client.post(
        "/api/documents/",
        files=files,
        data={"course_id": _course_id(client, trainer_headers, "CS101"), "class_id": class_a},
        headers=trainer_headers,
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
        "/api/documents/",
        files=files,
        data={"course_id": _course_id(client, trainer_headers, "CS101"), "class_id": class_a},
        headers=trainer_headers,
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
        "/api/documents/",
        files=files,
        data={"course_id": _course_id(client, trainer_headers, "CS101"), "class_id": class_a},
        headers=trainer_headers,
    )
    assert response.status_code == 201
    assert "not a real PDF" in response.json()["uploaded"][0]["error"]


def test_renamed_txt_to_docx_is_rejected(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    files = [("files", ("fake.docx", b"plain text, not zip", "application/vnd.openxmlformats"))]
    response = client.post(
        "/api/documents/",
        files=files,
        data={"course_id": _course_id(client, trainer_headers, "CS101"), "class_id": class_a},
        headers=trainer_headers,
    )
    assert response.status_code == 201
    assert "not a real .docx" in response.json()["uploaded"][0]["error"]


def test_oversize_upload_is_rejected():
    old = document_service.MAX_FILE_SIZE_BYTES
    document_service.MAX_FILE_SIZE_BYTES = 10
    try:
        error = document_service.validate_file("big.pdf", b"%PDF-" + b"x" * 20)
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
    assert "PHY101-C" not in mine
    # Admin sees every class.
    seen = {
        c["code"] for c in client.get("/api/documents/classes/mine", headers=admin_headers).json()
    }
    assert {"CS101-A", "CS101-B", "CS101-C", "PHY101-C"} <= seen
    assert client.get("/api/documents/classes/mine", headers=student_headers).status_code == 403


def test_list_search_filter_pagination_and_scope(
    db_session, client, trainer_headers, other_trainer_headers, student_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    other = db_session.query(User).filter(User.email == "other-teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    class_b = db_session.query(Class).filter(Class.code == "CS101-B").first()
    course = db_session.query(Course).filter(Course.code == "CS101").first()
    document_service.create_document(db_session, "algebra.pdf", b"%PDF-a", class_a.id, teacher)
    document_service.create_document(db_session, "algebra-notes.txt", b"notes", class_a.id, teacher)
    document_service.create_document(db_session, "physics.pdf", b"%PDF-p", class_b.id, teacher)

    # Course-scoped: any trainer of CS101 shares the same document pool.
    other_rows, other_total = document_service.list_documents(db_session, other)
    assert other_total == 3
    assert "algebra.pdf" in {r.file_name for r in other_rows}

    _, teacher_total = document_service.list_documents(db_session, teacher)
    assert teacher_total == 3

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

    # Course filter narrows to one course (List AC: course-scoped view).
    by_course = client.get(
        "/api/documents/", params={"course_id": str(course.id)}, headers=trainer_headers
    ).json()
    assert by_course["total"] == 3

    page1 = client.get(
        "/api/documents/", params={"page": 1, "page_size": 2}, headers=trainer_headers
    ).json()
    page2 = client.get(
        "/api/documents/", params={"page": 2, "page_size": 2}, headers=trainer_headers
    ).json()
    assert page1["total"] == 3 and len(page1["items"]) == 2 and len(page2["items"]) == 1

    row = page1["items"][0]
    assert {
        "file_name",
        "file_type",
        "size_bytes",
        "uploader_id",
        "class_id",
        "created_at",
        "course_id",
        "version_no",
    } <= set(row)

    assert client.get("/api/documents/", headers=student_headers).status_code == 403


def test_upload_to_unassigned_course_is_rejected(
    db_session, client, trainer_headers, outsider_headers, student_headers
):
    other = db_session.query(User).filter(User.email == "other-teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    # Service-level scope check still enforced.
    with pytest.raises(ValueError, match="not assigned"):
        document_service.create_document(
            db_session, "a.pdf", b"%PDF-1.4 a", class_a.id, other, scope="class"
        )
    # API-level: a trainer of another course gets a per-file error (201).
    course_id = _course_id(client, trainer_headers, "CS101")
    files = [("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf"))]
    denied = client.post(
        "/api/documents/", files=files, data={"course_id": course_id}, headers=outsider_headers
    )
    assert denied.status_code == 201
    assert "not assigned" in denied.json()["uploaded"][0]["error"]
    forbidden = client.post(
        "/api/documents/", files=files, data={"course_id": course_id}, headers=student_headers
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


def test_same_course_trainer_can_approve(
    db_session, client, trainer_headers, other_trainer_headers
):
    """D2: every trainer of the course may review, sharing the document pool."""
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    response = client.post(f"/api/documents/{record.id}/approve", headers=other_trainer_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "processing"


def test_approve_other_course_is_not_found(
    db_session, client, trainer_headers, outsider_headers
):
    """D6: a trainer outside the document's course gets 404 (no existence leak)."""
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    response = client.post(f"/api/documents/{record.id}/approve", headers=outsider_headers)
    assert response.status_code == 404


def test_reject_pending_with_reason(db_session, client, trainer_headers):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "r.pdf", b"%PDF-r", class_a.id, teacher)
    response = client.post(
        f"/api/documents/{record.id}/reject",
        json={"reason": "Outdated edition."},
        headers=trainer_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["review_note"] == "Outdated edition."


def test_reject_non_pending_is_bad_request(db_session, client, trainer_headers):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "r.pdf", b"%PDF-r", class_a.id, teacher)
    assert client.post(f"/api/documents/{record.id}/approve", headers=trainer_headers).status_code == 200
    response = client.post(f"/api/documents/{record.id}/reject", headers=trainer_headers)
    assert response.status_code == 400


def test_reject_other_course_and_student_forbidden(
    db_session, client, trainer_headers, outsider_headers, student_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "r.pdf", b"%PDF-r", class_a.id, teacher)
    assert (
        client.post(f"/api/documents/{record.id}/reject", headers=outsider_headers).status_code
        == 404
    )
    assert (
        client.post(f"/api/documents/{record.id}/reject", headers=student_headers).status_code
        == 403
    )
    assert client.post(f"/api/documents/{record.id}/reject").status_code in (401, 403)


def test_reject_unknown_id_is_not_found(client, trainer_headers):
    response = client.post(
        "/api/documents/00000000-0000-0000-0000-000000000000/reject", headers=trainer_headers
    )
    assert response.status_code == 404


def test_qdrant_payload_scopes_by_class(db_session, client, trainer_headers):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    payload = document_service.build_qdrant_payload(record)
    assert payload["class_id"] == str(class_a.id)
    assert payload["document_id"] == str(record.id)
    assert uuid.UUID(payload["document_id"])


# --- Phase 2b: course-shared versioning -------------------------------------


def _upload(client, headers, class_id, name, content, **data):
    rows = client.get("/api/documents/classes/mine", headers=headers).json()
    course_id = next(c["course_id"] for c in rows if c["id"] == class_id)
    files = [("files", (name, content, "application/pdf"))]
    payload = {"course_id": course_id, "class_id": class_id, **data}
    return client.post("/api/documents/", files=files, data=payload, headers=headers)


def test_reupload_same_name_creates_next_version_and_supersedes_pending(
    client, trainer_headers
):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    first = _upload(client, trainer_headers, class_a, "plan.pdf", b"%PDF-one").json()
    first_doc = first["uploaded"][0]["document"]
    assert first_doc["version_no"] == 1

    second = _upload(client, trainer_headers, class_a, "plan.pdf", b"%PDF-two").json()
    second_doc = second["uploaded"][0]["document"]
    assert second_doc["id"] == first_doc["id"]  # same version group
    assert second_doc["version_no"] == 2

    versions = client.get(
        f"/api/documents/{first_doc['id']}/versions", headers=trainer_headers
    ).json()["items"]
    assert [v["version_no"] for v in versions] == [2, 1]
    by_no = {v["version_no"]: v for v in versions}
    assert by_no[1]["status"] == "superseded"
    assert by_no[2]["status"] == "pending"


def test_duplicate_content_is_warned_not_blocked(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    _upload(client, trainer_headers, class_a, "dup.pdf", b"%PDF-same")
    again = _upload(client, trainer_headers, class_a, "dup.pdf", b"%PDF-same").json()
    result = again["uploaded"][0]
    assert result["error"] is None
    assert result["document"]["version_no"] == 2
    assert result["warning"] and "Identical content" in result["warning"]


def test_approve_switches_current_version_atomically(db_session):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(
        db_session, "handbook.pdf", b"%PDF-v1", class_a.id, teacher
    )
    document_service.approve_document(db_session, record.id, reviewer=teacher)
    v1 = document_service.versions_for(db_session, record.id)[0]
    v1.status = "indexed"  # AI-55 pipeline finishes indexing
    db_session.commit()

    document_service.create_document(db_session, "handbook.pdf", b"%PDF-v2", class_a.id, teacher)
    document_service.approve_document(db_session, record.id, reviewer=teacher)

    versions = {v.version_no: v for v in document_service.versions_for(db_session, record.id)}
    assert versions[1].status == "superseded"
    assert versions[2].status == "processing"
    db_session.refresh(record)
    assert record.current_version_id == versions[2].id
    assert record.status == "processing"


def test_reject_latest_keeps_previous_serving_version(db_session):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "keep.pdf", b"%PDF-v1", class_a.id, teacher)
    document_service.approve_document(db_session, record.id, reviewer=teacher)
    v1 = document_service.versions_for(db_session, record.id)[0]
    v1.status = "indexed"
    db_session.commit()

    document_service.create_document(db_session, "keep.pdf", b"%PDF-v2", class_a.id, teacher)
    document_service.reject_document(db_session, record.id, "not this one", reviewer=teacher)

    versions = {v.version_no: v for v in document_service.versions_for(db_session, record.id)}
    assert versions[1].status == "indexed"  # still serving
    assert versions[2].status == "rejected"
    db_session.refresh(record)
    assert record.status == "rejected"  # row mirrors the newest version
    assert record.current_version_id == versions[1].id


def test_shared_upload_across_classes_is_one_group(db_session):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    class_b = db_session.query(Class).filter(Class.code == "CS101-B").first()
    first = document_service.create_document(db_session, "syllabus.pdf", b"%PDF-a", class_a.id, teacher)
    second = document_service.create_document(
        db_session, "Syllabus.PDF", b"%PDF-b", class_b.id, teacher
    )
    assert second.id == first.id  # course-wide group, not a clash
    assert document_service.latest_version_no(db_session, first.id) == 2
    assert second.needs_review is False


def test_versions_of_out_of_course_document_is_not_found(
    db_session, client, outsider_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    response = client.get(f"/api/documents/{record.id}/versions", headers=outsider_headers)
    assert response.status_code == 404


# --- Phase 2c: rollback / retry / hide / share / delete / audit --------------


def _two_versions(db_session, name="rb.pdf"):
    """v1 approved+indexed, then v2 approved and serving (v1 superseded)."""

    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, name, b"%PDF-v1", class_a.id, teacher)
    document_service.approve_document(db_session, record.id, reviewer=teacher)
    v1 = document_service.versions_for(db_session, record.id)[0]
    v1.status = "indexed"
    db_session.commit()
    document_service.create_document(db_session, name, b"%PDF-v2", class_a.id, teacher)
    document_service.approve_document(db_session, record.id, reviewer=teacher)
    return teacher, record


def test_rollback_serves_older_version(db_session):
    teacher, record = _two_versions(db_session)
    document_service.rollback_document(db_session, record.id, 1, reviewer=teacher)
    versions = {v.version_no: v for v in document_service.versions_for(db_session, record.id)}
    assert versions[1].status == "processing"
    assert versions[2].status == "superseded"
    db_session.refresh(record)
    assert record.current_version_id == versions[1].id


def test_rollback_unknown_version_is_not_found(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    doc = _upload(client, trainer_headers, class_a, "rb.pdf", b"%PDF-1").json()["uploaded"][0][
        "document"
    ]
    response = client.post(
        f"/api/documents/{doc['id']}/rollback", json={"version_no": 99}, headers=trainer_headers
    )
    assert response.status_code == 404


def test_retry_failed_version(db_session):
    teacher, record = _two_versions(db_session, "rt.pdf")
    v2 = document_service.versions_for(db_session, record.id)[0]
    v2.status = "failed"
    db_session.commit()

    document_service.retry_document(db_session, record.id, reviewer=teacher)

    v2 = document_service.versions_for(db_session, record.id)[0]
    assert v2.status == "processing"
    db_session.refresh(record)
    assert record.current_version_id == v2.id


def test_retry_without_failed_version_is_bad_request(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    doc = _upload(client, trainer_headers, class_a, "rt.pdf", b"%PDF-1").json()["uploaded"][0][
        "document"
    ]
    assert client.post(f"/api/documents/{doc['id']}/retry", headers=trainer_headers).status_code == 400


def test_upload_picker_lists_course_classes(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    course_id = client.get("/api/documents/classes/mine", headers=trainer_headers).json()
    course_id = next(c["course_id"] for c in course_id if c["id"] == class_a)
    classes = client.get(
        f"/api/documents/courses/{course_id}/classes", headers=trainer_headers
    ).json()
    codes = {c["code"] for c in classes}
    assert {"CS101-A", "CS101-B"} <= codes


def test_student_sees_course_shared_document_of_another_class(
    db_session, client, trainer_headers, student_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    class_b = db_session.query(Class).filter(Class.code == "CS101-B").first()
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    # The student sits in section B; the material is uploaded in section A.
    db_session.add(Enrollment(class_id=class_b.id, student_id=student.id, status="ACTIVE"))
    db_session.commit()

    record = document_service.create_document(
        db_session, "shared-notes.pdf", b"%PDF-cs", class_a.id, teacher
    )
    document_service.approve_document(db_session, record.id, reviewer=teacher)
    record.status = "indexed"
    db_session.commit()

    listed = client.get(
        f"/api/student/classes/{class_b.id}/documents", headers=student_headers
    ).json()
    assert "shared-notes.pdf" in {d["file_name"] for d in listed}

    # Removing it for section B hides it from B's students only.
    document_service.remove_from_class(db_session, record.id, class_b.id, actor=teacher)
    listed2 = client.get(
        f"/api/student/classes/{class_b.id}/documents", headers=student_headers
    ).json()
    assert "shared-notes.pdf" not in {d["file_name"] for d in listed2}


def test_delete_uploader_or_admin_only(
    db_session, client, trainer_headers, other_trainer_headers, admin_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "del.pdf", b"%PDF-d", class_a.id, teacher)

    # Same-course co-teacher may review but not delete someone else's document.
    denied = client.delete(f"/api/documents/{record.id}", headers=other_trainer_headers)
    assert denied.status_code == 403
    # Uploader can.
    assert (
        client.delete(f"/api/documents/{record.id}", headers=trainer_headers).status_code == 200
    )
    # Soft-deleted: hidden from list and no longer reachable.
    assert client.get(f"/api/documents/{record.id}/versions", headers=trainer_headers).status_code == 404

    # Admin can delete anyone's document.
    other_doc = document_service.create_document(
        db_session, "del2.pdf", b"%PDF-d2", class_a.id, teacher
    )
    assert client.delete(f"/api/documents/{other_doc.id}", headers=admin_headers).status_code == 200


def test_events_trail_records_upload_and_approve(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    doc = _upload(client, trainer_headers, class_a, "events.pdf", b"%PDF-e").json()["uploaded"][0][
        "document"
    ]
    client.post(f"/api/documents/{doc['id']}/approve", headers=trainer_headers)

    events = client.get(
        f"/api/documents/{doc['id']}/events", headers=trainer_headers
    ).json()["items"]
    actions = {e["action"] for e in events}
    assert {"upload", "approve"} <= actions


# --- Model C per-class opt-out (AI-37): remove from one class only -----------


def _list_by_id(client, headers, class_id, **params):
    rows = client.get(
        "/api/documents/",
        params={"class_id": class_id, **params},
        headers=headers,
    ).json()["items"]
    return {i["id"]: i for i in rows}


def test_remove_from_class_hides_only_that_class(
    client, trainer_headers, other_trainer_headers
):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    class_c = _class_id(client, other_trainer_headers, "CS101-C")
    doc = _upload(client, trainer_headers, class_a, "course-notes.pdf", b"%PDF-o").json()[
        "uploaded"
    ][0]["document"]
    doc_id = doc["id"]
    assert doc_id in _list_by_id(client, other_trainer_headers, class_c)

    removed = client.delete(
        f"/api/documents/{doc_id}/classes/{class_c}", headers=other_trainer_headers
    )
    assert removed.status_code == 200

    # Only CS101-C lost it; the material stays in the subject for CS101-A.
    assert doc_id not in _list_by_id(client, other_trainer_headers, class_c)
    assert doc_id in _list_by_id(client, trainer_headers, class_a)

    # include_removed surfaces the row with the flag, so the UI can Restore.
    with_removed = _list_by_id(client, other_trainer_headers, class_c, include_removed=True)
    assert with_removed[doc_id]["removed_for_class"] is True

    restored = client.put(
        f"/api/documents/{doc_id}/classes/{class_c}", headers=other_trainer_headers
    )
    assert restored.status_code == 200
    back = _list_by_id(client, other_trainer_headers, class_c)
    assert doc_id in back
    assert back[doc_id]["removed_for_class"] is False


def test_remove_from_class_out_of_course_is_not_found(
    client, trainer_headers, outsider_headers
):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    doc = _upload(client, trainer_headers, class_a, "x.pdf", b"%PDF-x").json()["uploaded"][0][
        "document"
    ]
    # A trainer of another course cannot even see the document (D6: no leak).
    assert (
        client.delete(
            f"/api/documents/{doc['id']}/classes/{class_a}", headers=outsider_headers
        ).status_code
        == 404
    )
    # A class outside the document's course is rejected as well.
    physics = _class_id(client, outsider_headers, "PHY101-C")
    assert (
        client.delete(
            f"/api/documents/{doc['id']}/classes/{physics}", headers=trainer_headers
        ).status_code
        == 404
    )


def test_reupload_after_removal_brings_class_back(db_session):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(
        db_session, "back.pdf", b"%PDF-1", class_a.id, teacher
    )
    document_service.remove_from_class(db_session, record.id, class_a.id, actor=teacher)
    assert document_service.is_removed_for_class(db_session, record.id, class_a.id) is True

    # Uploading a new version from the class re-subscribes it.
    document_service.create_document(db_session, "back.pdf", b"%PDF-2", class_a.id, teacher)
    assert document_service.is_removed_for_class(db_session, record.id, class_a.id) is False


def test_removed_class_student_does_not_see_document(
    db_session, client, trainer_headers, student_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_b = db_session.query(Class).filter(Class.code == "CS101-B").first()
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    db_session.add(Enrollment(class_id=class_b.id, student_id=student.id, status="ACTIVE"))
    db_session.commit()

    record = document_service.create_document(
        db_session, "optout.pdf", b"%PDF-oo", class_b.id, teacher
    )
    document_service.approve_document(db_session, record.id, reviewer=teacher)
    record.status = "indexed"
    db_session.commit()
    assert "optout.pdf" in {
        d["file_name"]
        for d in client.get(
            f"/api/student/classes/{class_b.id}/documents", headers=student_headers
        ).json()
    }

    document_service.remove_from_class(db_session, record.id, class_b.id, actor=teacher)
    assert "optout.pdf" not in {
        d["file_name"]
        for d in client.get(
            f"/api/student/classes/{class_b.id}/documents", headers=student_headers
        ).json()
    }


# --- Class-only materials: a class uploads its own extra files ---------------


def _upload_class(client, headers, class_id, name, content):
    """Upload class-private material from the class page (course is derived)."""

    files = [("files", (name, content, "application/pdf"))]
    return client.post(
        "/api/documents/",
        files=files,
        data={"class_id": class_id, "scope": "class"},
        headers=headers,
    )


def test_class_material_is_private_to_its_class(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    class_b = _class_id(client, trainer_headers, "CS101-B")
    result = _upload_class(client, trainer_headers, class_a, "extra-a.pdf", b"%PDF-x").json()[
        "uploaded"
    ][0]
    assert result["error"] is None
    doc = result["document"]
    assert doc["scope"] == "class"
    assert doc["class_id"] == class_a

    assert doc["id"] in _list_by_id(client, trainer_headers, class_a)
    assert doc["id"] not in _list_by_id(client, trainer_headers, class_b)


def test_class_material_hidden_from_subject_course_list(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    course_id = _course_id(client, trainer_headers, "CS101")
    doc = _upload_class(client, trainer_headers, class_a, "only-a.pdf", b"%PDF-x").json()[
        "uploaded"
    ][0]["document"]

    subject = client.get(
        "/api/documents/",
        params={"course_id": course_id, "scope": "course"},
        headers=trainer_headers,
    ).json()
    assert doc["id"] not in {i["id"] for i in subject["items"]}

    class_only = client.get(
        "/api/documents/", params={"scope": "class"}, headers=trainer_headers
    ).json()
    assert doc["id"] in {i["id"] for i in class_only["items"]}


def test_class_material_only_its_trainer_can_review(
    client, trainer_headers, other_trainer_headers
):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    class_c = _class_id(client, other_trainer_headers, "CS101-C")
    doc = _upload_class(client, trainer_headers, class_a, "rev.pdf", b"%PDF-x").json()[
        "uploaded"
    ][0]["document"]

    # Another trainer of the same subject cannot see or review it.
    assert doc["id"] not in _list_by_id(client, other_trainer_headers, class_c)
    denied = client.post(
        f"/api/documents/{doc['id']}/approve", headers=other_trainer_headers
    )
    assert denied.status_code == 404

    ok = client.post(f"/api/documents/{doc['id']}/approve", headers=trainer_headers)
    assert ok.status_code == 200
    assert ok.json()["scope"] == "class"


def test_class_material_upload_requires_assigned_class(client, trainer_headers, outsider_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    response = _upload_class(client, outsider_headers, class_a, "nope.pdf", b"%PDF-x")
    assert response.status_code == 201
    assert "not assigned" in response.json()["uploaded"][0]["error"]


def test_remove_from_class_rejects_class_material(client, trainer_headers):
    class_a = _class_id(client, trainer_headers, "CS101-A")
    doc = _upload_class(client, trainer_headers, class_a, "del-me.pdf", b"%PDF-x").json()[
        "uploaded"
    ][0]["document"]
    response = client.delete(
        f"/api/documents/{doc['id']}/classes/{class_a}", headers=trainer_headers
    )
    assert response.status_code == 400


def test_student_sees_only_own_class_material(
    db_session, client, trainer_headers, student_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    class_b = db_session.query(Class).filter(Class.code == "CS101-B").first()
    student = db_session.query(User).filter(User.email == "student@academic.edu.vn").first()
    db_session.add(Enrollment(class_id=class_b.id, student_id=student.id, status="ACTIVE"))
    db_session.commit()

    a_doc = document_service.create_document(
        db_session, "a-only.pdf", b"%PDF-a", class_a.id, teacher, scope="class"
    )
    b_doc = document_service.create_document(
        db_session, "b-only.pdf", b"%PDF-b", class_b.id, teacher, scope="class"
    )
    for rec in (a_doc, b_doc):
        document_service.approve_document(db_session, rec.id, reviewer=teacher)
        rec.status = "indexed"
    db_session.commit()

    names = {
        d["file_name"]
        for d in client.get(
            f"/api/student/classes/{class_b.id}/documents", headers=student_headers
        ).json()
    }
    assert "b-only.pdf" in names
    assert "a-only.pdf" not in names
