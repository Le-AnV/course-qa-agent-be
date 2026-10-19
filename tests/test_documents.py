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


def test_accepted_types_lists_six_material_formats(client, trainer_headers):
    response = client.get("/api/documents/accepted-types", headers=trainer_headers)
    assert response.status_code == 200
    extensions = {t["extension"] for t in response.json()}
    assert extensions == {".pdf", ".docx", ".md", ".txt", ".pptx", ".xlsx"}
    assert ".csv" not in extensions


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


# --- AI-39: delete & replace documents --------------------------------------


class _Chunk:
    """Retrieved chunk stand-in carrying the Qdrant payload metadata."""

    def __init__(self, document_id: str | None = None) -> None:
        self.metadata = {} if document_id is None else {"document_id": document_id}


def _upload(client, headers, class_id, name, content=b"%PDF-1.4 x", mime="application/pdf"):
    files = [("files", (name, content, mime))]
    return client.post("/api/documents/", files=files, data={"class_id": class_id}, headers=headers)


def test_delete_requires_confirmation_and_purges_vectors(
    db_session, client, trainer_headers, monkeypatch
):
    """AI-39 AC-01: confirm is mandatory; the file + its chunks are removed."""
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "old.pdf", b"%PDF-a", class_a.id, teacher)
    record.status = "indexed"
    db_session.commit()

    purged_ids: list[str] = []

    def fake_purge(document_id):
        purged_ids.append(str(document_id))
        return 7

    monkeypatch.setattr(document_service, "purge_document_vectors", fake_purge)

    # AC-01: no confirmation, nothing happens.
    unconfirmed = client.delete(f"/api/documents/{record.id}", headers=trainer_headers)
    assert unconfirmed.status_code == 400
    assert "confirm" in unconfirmed.json()["detail"]
    assert document_service.get_document(db_session, record.id) is not None

    response = client.delete(f"/api/documents/{record.id}?confirm=true", headers=trainer_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == str(record.id)
    assert body["file_name"] == "old.pdf"
    assert body["purged_chunks"] == 7
    assert purged_ids == [str(record.id)]

    assert document_service.get_document(db_session, record.id) is None
    listed = client.get(
        "/api/documents/", params={"search": "old.pdf"}, headers=trainer_headers
    ).json()
    assert listed["total"] == 0
    again = client.delete(f"/api/documents/{record.id}?confirm=true", headers=trainer_headers)
    assert again.status_code == 404


def test_delete_pending_document_skips_vector_purge(
    db_session, client, trainer_headers, monkeypatch
):
    """A never-indexed file has no chunks: delete must not need Qdrant."""
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "new.pdf", b"%PDF-n", class_a.id, teacher)

    called: list[str] = []
    monkeypatch.setattr(
        document_service, "purge_document_vectors", lambda document_id: called.append(str(document_id))
    )
    response = client.delete(f"/api/documents/{record.id}?confirm=true", headers=trainer_headers)
    assert response.status_code == 200
    assert response.json()["purged_chunks"] == 0
    assert called == []


def test_delete_is_refused_when_qdrant_is_unreachable(
    db_session, client, trainer_headers, monkeypatch
):
    """AI-39 AC-02: never report 'deleted' while chunks can still answer."""
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "live.pdf", b"%PDF-l", class_a.id, teacher)
    record.status = "indexed"
    db_session.commit()

    def boom(document_id):
        raise document_service.VectorPurgeError("qdrant down")

    monkeypatch.setattr(document_service, "purge_document_vectors", boom)
    response = client.delete(f"/api/documents/{record.id}?confirm=true", headers=trainer_headers)
    assert response.status_code == 503
    assert "qdrant down" in response.json()["detail"]
    assert document_service.get_document(db_session, record.id) is not None


def test_delete_scope_blocks_other_trainer_and_student(
    db_session, client, trainer_headers, other_trainer_headers, student_headers
):
    """AI-39 AC-04: only the owner's class is deletable; students never."""
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "own.pdf", b"%PDF-o", class_a.id, teacher)

    forbidden = client.delete(f"/api/documents/{record.id}?confirm=true", headers=other_trainer_headers)
    assert forbidden.status_code == 403
    assert document_service.get_document(db_session, record.id) is not None

    student = client.delete(f"/api/documents/{record.id}?confirm=true", headers=student_headers)
    assert student.status_code == 403

    unknown = client.delete(f"/api/documents/{uuid.uuid4()}?confirm=true", headers=trainer_headers)
    assert unknown.status_code == 404


def test_replace_same_name_creates_next_version_and_reindexes(
    db_session, client, trainer_headers, monkeypatch
):
    """AI-39 AC-03: same name = new version, old chunks purged, re-indexed."""
    class_a = _class_id(client, trainer_headers, "CS101-A")
    purged_ids: list[str] = []

    def fake_purge(document_id):
        purged_ids.append(str(document_id))
        return 4

    monkeypatch.setattr(document_service, "purge_document_vectors", fake_purge)

    first = _upload(client, trainer_headers, class_a, "syllabus.pdf")
    first_doc = first.json()["uploaded"][0]["document"]
    assert first_doc["version"] == 1
    assert first_doc["status"] == "pending"
    approved = client.post(f"/api/documents/{first_doc['id']}/approve", headers=trainer_headers)
    assert approved.status_code == 200

    second = _upload(
        client, trainer_headers, class_a, "syllabus.pdf", content=b"%PDF-2.0 revision"
    )
    second_doc = second.json()["uploaded"][0]["document"]
    assert second_doc["version"] == 2
    assert second_doc["status"] == "processing"  # re-indexed without re-approval
    assert purged_ids == [first_doc["id"]]

    listed = client.get(
        "/api/documents/", params={"search": "syllabus"}, headers=trainer_headers
    ).json()
    assert listed["total"] == 2
    by_id = {row["id"]: row for row in listed["items"]}
    assert by_id[first_doc["id"]]["status"] == "replaced"
    assert by_id[second_doc["id"]]["version"] == 2

    # The superseded version is still deletable (purge already happened, so
    # deleting it must not touch Qdrant again).
    frees = client.delete(f"/api/documents/{first_doc['id']}?confirm=true", headers=trainer_headers)
    assert frees.status_code == 200
    assert frees.json()["purged_chunks"] == 0
    assert purged_ids == [first_doc["id"]]


def test_replace_versions_are_scoped_per_class(client, trainer_headers, monkeypatch):
    """AI-39 AC-03/AC-04: versioning is per class, never across trainers."""
    class_a = _class_id(client, trainer_headers, "CS101-A")
    class_b = _class_id(client, trainer_headers, "CS101-B")
    called: list[str] = []
    monkeypatch.setattr(
        document_service, "purge_document_vectors", lambda document_id: called.append(str(document_id))
    )

    a1 = _upload(client, trainer_headers, class_a, "notes.txt", b"notes a", "text/plain")
    b1 = _upload(client, trainer_headers, class_b, "notes.txt", b"notes b", "text/plain")
    assert a1.json()["uploaded"][0]["document"]["version"] == 1
    assert b1.json()["uploaded"][0]["document"]["version"] == 1

    # Same name uploaded again in class A: v2. Class B stays at v1.
    a2 = _upload(client, trainer_headers, class_a, "notes.txt", b"notes a v2", "text/plain")
    assert a2.json()["uploaded"][0]["document"]["version"] == 2
    b2 = _upload(client, trainer_headers, class_b, "notes.txt", b"notes b v2", "text/plain")
    assert b2.json()["uploaded"][0]["document"]["version"] == 2
    # Both previous rows were still pending (no chunks) → no purge needed.
    assert called == []


def test_failed_replace_keeps_old_version(client, trainer_headers, monkeypatch):
    """A replace aborts when the outdated chunks cannot be purged."""
    class_a = _class_id(client, trainer_headers, "CS101-A")
    first = _upload(client, trainer_headers, class_a, "keep.pdf")
    first_doc = first.json()["uploaded"][0]["document"]
    client.post(f"/api/documents/{first_doc['id']}/approve", headers=trainer_headers)

    def boom(document_id):
        raise document_service.VectorPurgeError("qdrant down")

    monkeypatch.setattr(document_service, "purge_document_vectors", boom)
    response = _upload(client, trainer_headers, class_a, "keep.pdf", content=b"%PDF-2.0")
    assert response.status_code == 201
    result = response.json()["uploaded"][0]
    assert result["document"] is None
    assert result["error"] == "qdrant down"

    rows = client.get(
        "/api/documents/", params={"search": "keep.pdf"}, headers=trainer_headers
    ).json()
    assert rows["total"] == 1  # no v2 row was persisted
    assert rows["items"][0]["status"] == "processing"  # old version untouched
    assert rows["items"][0]["version"] == 1


def test_deleted_and_replaced_chunks_are_not_retrievable(
    db_session, client, trainer_headers, monkeypatch
):
    """AI-39 AC-02: the retrieval guard drops chunks of dead documents."""
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    live = document_service.create_document(db_session, "live.pdf", b"%PDF-l", class_a.id, teacher)
    gone = document_service.create_document(db_session, "gone.pdf", b"%PDF-g", class_a.id, teacher)
    superseded = document_service.create_document(
        db_session, "old-rev.pdf", b"%PDF-o", class_a.id, teacher
    )
    for record in (live, gone, superseded):
        record.status = "indexed"
    db_session.commit()

    chunks = [
        _Chunk(str(live.id)),
        _Chunk(str(gone.id)),
        _Chunk(str(superseded.id)),
        _Chunk(),  # unverifiable payload: dropped
    ]
    monkeypatch.setattr(document_service, "purge_document_vectors", lambda document_id: 1)
    assert (
        client.delete(f"/api/documents/{gone.id}?confirm=true", headers=trainer_headers).status_code
        == 200
    )
    superseded.status = "replaced"
    db_session.commit()

    assert document_service.filter_retrievable_chunks(db_session, class_a.id, chunks) == [chunks[0]]


def test_list_accepts_replaced_status_filter(client, trainer_headers):
    """The replaced status is a valid list filter after AI-39."""
    response = client.get(
        "/api/documents/", params={"status": "replaced"}, headers=trainer_headers
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0
