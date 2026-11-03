"""Tests for Epic E2: upload / list / tag documents (AI-35/37/41).

Auth (AI-25/27): endpoints require `Authorization: Bearer <JWT>` for a
DB-backed login token (trainer+admin allowed, student denied, missing
token = 401). Uses in-memory SQLite (StaticPool) so CI stays DB-free.
"""

import io
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.security import hash_password
from app.core.seed import seed_database
from app.main import app
from app.models.course import Class, ClassTrainer
from app.models.user import Account, Role, User
from app.services import document_service, ingestion_service

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)
    monkeypatch.setattr(settings, "email_dry_run", True)
    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)

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
def client(db_session, monkeypatch):
    # Route-level lifecycle tests exercise scheduling without calling Gemini/Qdrant.
    from app.services import ingestion_service

    monkeypatch.setattr(ingestion_service, "process_document_in_background", lambda _id: None)

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


def test_approve_trainer_only(db_session, client, trainer_headers, student_headers, monkeypatch):
    from app.services import ingestion_service

    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    scheduled = []
    monkeypatch.setattr(
        ingestion_service,
        "process_document_in_background",
        lambda document_id: scheduled.append(document_id),
    )
    forbidden = client.post(f"/api/documents/{record.id}/approve", headers=student_headers)
    assert forbidden.status_code == 403
    response = client.post(f"/api/documents/{record.id}/approve", headers=trainer_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    assert scheduled == [str(record.id)]


def test_reject_pending_document_and_prevent_invalid_transitions(
    db_session, client, trainer_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(
        db_session, "reject.txt", b"lesson", class_a.id, teacher
    )

    response = client.post(f"/api/documents/{record.id}/reject", headers=trainer_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    again = client.post(f"/api/documents/{record.id}/reject", headers=trainer_headers)
    assert again.status_code == 400


def test_retry_failed_document_schedules_ingestion(
    db_session, client, trainer_headers, monkeypatch
):
    from app.services import ingestion_service

    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(
        db_session, "retry.txt", b"lesson", class_a.id, teacher
    )
    record.status = "failed"
    record.failure_reason = "Gemini embedding quota exceeded."
    db_session.commit()
    scheduled = []
    monkeypatch.setattr(
        ingestion_service,
        "process_document_in_background",
        lambda document_id: scheduled.append(document_id),
    )

    response = client.post(f"/api/documents/{record.id}/retry", headers=trainer_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    assert response.json()["failure_reason"] is None
    assert scheduled == [str(record.id)]
    again = client.post(f"/api/documents/{record.id}/retry", headers=trainer_headers)
    assert again.status_code == 400


def test_fail_stale_processing_documents_marks_them_failed(db_session):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "stale.txt", b"lesson", class_a.id, teacher)
    record.status = "processing"
    db_session.commit()

    recovered = document_service.fail_stale_processing(db_session)

    assert recovered == 1
    assert record.status == "failed"
    assert "server restart" in record.failure_reason.lower()


def test_document_path_rejects_paths_outside_storage_root(monkeypatch, tmp_path):
    storage_root = tmp_path / "documents"
    outside_path = tmp_path / "outside.txt"
    monkeypatch.setattr(settings, "document_storage_dir", storage_root)

    with pytest.raises(FileNotFoundError):
        document_service.document_path(SimpleNamespace(storage_path=str(outside_path)))


def test_relative_storage_dir_round_trips(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "document_storage_dir", Path("./storage/documents"))

    path = document_service.store_upload_file(uuid.uuid4(), "lesson.txt", b"x")

    assert path.is_absolute()
    assert document_service.document_path(SimpleNamespace(storage_path=str(path))).is_file()
    legacy_relative_path = path.relative_to(tmp_path)
    assert document_service.document_path(
        SimpleNamespace(storage_path=str(legacy_relative_path))
    ).is_file()


def test_store_upload_file_removes_partial_file_on_write_error(monkeypatch, tmp_path):
    document_id = uuid.uuid4()
    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    original_write = Path.write_bytes

    def partial_write_then_fail(path, content):
        if path.name == f"{document_id}.txt":
            original_write(path, b"partial")
            raise OSError("disk full")
        return original_write(path, content)

    monkeypatch.setattr(Path, "write_bytes", partial_write_then_fail)

    with pytest.raises(OSError, match="disk full"):
        document_service.store_upload_file(document_id, "lesson.txt", b"lesson")
    assert not (tmp_path / f"{document_id}.txt").exists()


def test_replacement_keeps_indexed_version_until_new_version_finishes(
    db_session, client, trainer_headers, monkeypatch
):
    class_id = _class_id(client, trainer_headers, "CS101-A")
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    old = document_service.create_document(
        db_session, "syllabus.txt", b"old content", class_id, teacher
    )
    old.status = document_service.STATUS_INDEXED
    db_session.commit()

    replacement = document_service.create_document(
        db_session, "syllabus.txt", b"new content", class_id, teacher
    )

    assert replacement.version == 2
    assert replacement.status == document_service.STATUS_PENDING
    assert old.status == document_service.STATUS_INDEXED
    replacement.status = document_service.STATUS_PROCESSING
    db_session.commit()

    purged: list[str] = []
    monkeypatch.setattr(
        document_service,
        "purge_document_vectors",
        lambda document_id: purged.append(str(document_id)) or 2,
    )
    document_service.supersede_older_versions(db_session, replacement)

    assert old.status == document_service.STATUS_REPLACED
    assert replacement.status == document_service.STATUS_INDEXED
    assert purged == [str(old.id)]
    assert not document_service.document_path(old).exists()


def test_failed_replacement_does_not_supersede_indexed_version(
    db_session, client, trainer_headers, monkeypatch
):
    class_id = _class_id(client, trainer_headers, "CS101-A")
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    old = document_service.create_document(
        db_session, "syllabus.txt", b"old content", class_id, teacher
    )
    old.status = document_service.STATUS_INDEXED
    db_session.commit()
    replacement = document_service.create_document(
        db_session, "syllabus.txt", b"new content", class_id, teacher
    )
    document_service.approve_document(db_session, replacement.id)
    qdrant = MagicMock()
    qdrant.add_documents.side_effect = RuntimeError("Qdrant unavailable")
    purge = MagicMock()

    monkeypatch.setattr(document_service, "purge_document_vectors", purge)
    monkeypatch.setattr(
        ingestion_service, "_load_document", lambda _path: {"markdown": "new", "images": {}}
    )
    monkeypatch.setattr(ingestion_service, "QdrantStore", lambda embeddings: qdrant)
    monkeypatch.setattr(ingestion_service, "GeminiEmbedding", lambda **_: object())
    monkeypatch.setattr(
        ingestion_service,
        "markdown_chunker",
        lambda _text, metadata: [SimpleNamespace(page_content="new", metadata={})],
    )
    monkeypatch.setattr(
        ingestion_service, "recursive_chunker", lambda docs, *args, **kwargs: docs
    )
    supersede = MagicMock()
    monkeypatch.setattr(document_service, "supersede_older_versions", supersede)

    ingestion_service.process_document(db_session, replacement.id)

    assert replacement.status == "failed"
    assert old.status == "indexed"
    supersede.assert_not_called()
    assert purge.call_args_list == [
        ((replacement.id,), {}),
        ((replacement.id,), {}),
    ]

def test_approve_other_class_is_forbidden(
    db_session, client, trainer_headers, other_trainer_headers
):
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(db_session, "s.pdf", b"%PDF-s", class_a.id, teacher)
    response = client.post(f"/api/documents/{record.id}/approve", headers=other_trainer_headers)
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("action", "initial_status"),
    (("approve", "pending"), ("reject", "pending"), ("retry", "failed")),
)
def test_only_uploader_can_manage_document(
    action,
    initial_status,
    db_session,
    client,
    other_trainer_headers,
):
    uploader = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    other = db_session.query(User).filter(User.email == "other-teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    db_session.add(ClassTrainer(class_id=class_a.id, trainer_id=other.id))
    record = document_service.create_document(
        db_session,
        f"uploader-only-{action}.txt",
        b"lesson",
        class_a.id,
        uploader,
    )
    record.status = initial_status
    db_session.commit()

    response = client.post(
        f"/api/documents/{record.id}/{action}",
        headers=other_trainer_headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Only the uploader can manage this document."


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
    assert not Path(record.storage_path).exists()


def test_delete_failed_document_purges_stale_vectors(
    db_session, client, trainer_headers, monkeypatch
):
    """A failed retry can leave partial chunks behind: delete must purge them."""
    teacher = db_session.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    class_a = db_session.query(Class).filter(Class.code == "CS101-A").first()
    record = document_service.create_document(
        db_session, "failed.pdf", b"%PDF-f", class_a.id, teacher
    )
    record.status = "failed"
    db_session.commit()

    purged_ids: list[str] = []

    def fake_purge(document_id):
        purged_ids.append(str(document_id))
        return 3

    monkeypatch.setattr(document_service, "purge_document_vectors", fake_purge)

    response = client.delete(f"/api/documents/{record.id}?confirm=true", headers=trainer_headers)
    assert response.status_code == 200
    assert response.json()["purged_chunks"] == 3
    assert purged_ids == [str(record.id)]
    assert document_service.get_document(db_session, record.id) is None


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
    """AI-39 AC-03: same name = new version; keep old version live while indexing."""
    class_a = _class_id(client, trainer_headers, "CS101-A")
    purged_ids: list[str] = []

    def fake_purge(document_id):
        purged_ids.append(str(document_id))
        return 4

    monkeypatch.setattr(document_service, "purge_document_vectors", fake_purge)
    scheduled: list[str] = []
    monkeypatch.setattr(
        ingestion_service,
        "process_document_in_background",
        lambda document_id: scheduled.append(document_id),
    )

    first = _upload(client, trainer_headers, class_a, "syllabus.pdf")
    first_doc = first.json()["uploaded"][0]["document"]
    assert first_doc["version"] == 1
    assert first_doc["status"] == "pending"
    approved = client.post(f"/api/documents/{first_doc['id']}/approve", headers=trainer_headers)
    assert approved.status_code == 200
    assert scheduled == [first_doc["id"]]
    scheduled.clear()
    old_record = db_session.query(models.Document).filter_by(id=uuid.UUID(first_doc["id"])).one()
    old_record.status = document_service.STATUS_INDEXED
    db_session.commit()

    second = _upload(
        client, trainer_headers, class_a, "syllabus.pdf", content=b"%PDF-2.0 revision"
    )
    second_doc = second.json()["uploaded"][0]["document"]
    assert second_doc["version"] == 2
    assert second_doc["status"] == "pending"  # replacement requires approval
    assert scheduled == []
    assert purged_ids == []

    listed = client.get(
        "/api/documents/", params={"search": "syllabus"}, headers=trainer_headers
    ).json()
    assert listed["total"] == 2
    by_id = {row["id"]: row for row in listed["items"]}
    assert by_id[first_doc["id"]]["status"] == "indexed"
    assert by_id[second_doc["id"]]["version"] == 2
    assert by_id[second_doc["id"]]["status"] == "pending"

    # The old version remains deletable and its live vectors are purged then.
    frees = client.delete(f"/api/documents/{first_doc['id']}?confirm=true", headers=trainer_headers)
    assert frees.status_code == 200
    assert frees.json()["purged_chunks"] == 4
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


def test_replace_does_not_purge_old_version_during_upload(
    db_session, client, trainer_headers, monkeypatch
):
    """The old indexed version remains live until replacement ingestion succeeds."""
    class_a = _class_id(client, trainer_headers, "CS101-A")
    first = _upload(client, trainer_headers, class_a, "keep.pdf")
    first_doc = first.json()["uploaded"][0]["document"]
    client.post(f"/api/documents/{first_doc['id']}/approve", headers=trainer_headers)
    old_record = db_session.query(models.Document).filter_by(id=uuid.UUID(first_doc["id"])).one()
    old_record.status = document_service.STATUS_INDEXED
    db_session.commit()

    def boom(document_id):
        raise document_service.VectorPurgeError("qdrant down")

    monkeypatch.setattr(document_service, "purge_document_vectors", boom)
    response = _upload(client, trainer_headers, class_a, "keep.pdf", content=b"%PDF-2.0")
    assert response.status_code == 201
    result = response.json()["uploaded"][0]
    assert result["document"]["version"] == 2
    assert result["document"]["status"] == "pending"

    rows = client.get(
        "/api/documents/", params={"search": "keep.pdf"}, headers=trainer_headers
    ).json()
    assert rows["total"] == 2
    by_id = {row["id"]: row for row in rows["items"]}
    assert by_id[first_doc["id"]]["status"] == "indexed"
    assert by_id[first_doc["id"]]["version"] == 1
    assert by_id[result["document"]["id"]]["status"] == "pending"


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
    processing = document_service.create_document(
        db_session, "processing.pdf", b"%PDF-p", class_a.id, teacher
    )
    for record in (live, gone, superseded):
        record.status = "indexed"
    processing.status = "processing"
    db_session.commit()

    chunks = [
        _Chunk(str(live.id)),
        _Chunk(str(gone.id)),
        _Chunk(str(superseded.id)),
        _Chunk(str(processing.id)),
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


# --- AI-43: preview & download ------------------------------------------------


def _uploaded_pdf(client, headers, name="lesson.pdf", content=b"%PDF-1.4 lesson"):
    class_a = _class_id(client, headers, "CS101-A")
    response = _upload(client, headers, class_a, name, content=content)
    assert response.status_code == 201
    return response.json()["uploaded"][0]["document"]


def test_download_returns_original_file_as_attachment(client, trainer_headers):
    """AI-43 AC-02: trainers download the exact uploaded bytes."""
    document = _uploaded_pdf(client, trainer_headers)

    response = client.get(f"/api/documents/{document['id']}/download", headers=trainer_headers)

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 lesson"
    assert response.headers["content-type"].startswith("application/pdf")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "lesson.pdf" in disposition


def test_preview_pdf_is_served_inline(client, trainer_headers):
    """AI-43 AC-01: a PDF previews in the browser instead of downloading."""
    document = _uploaded_pdf(client, trainer_headers)

    response = client.get(f"/api/documents/{document['id']}/preview", headers=trainer_headers)

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 lesson"
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith("inline")


def test_preview_image_is_served_inline(db_session, client, trainer_headers):
    """AI-43 AC-01: image materials preview too."""
    document = _uploaded_pdf(client, trainer_headers, name="diagram.pdf")
    record = db_session.query(models.Document).filter_by(id=uuid.UUID(document["id"])).one()
    record.file_name = "diagram.png"
    record.mime_type = "image/png"
    db_session.commit()

    response = client.get(f"/api/documents/{document['id']}/preview", headers=trainer_headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["content-disposition"].startswith("inline")


def test_preview_rejects_non_previewable_types(client, trainer_headers):
    """AI-43 AC-01: only PDFs and images preview; other formats download."""
    class_a = _class_id(client, trainer_headers, "CS101-A")
    response = _upload(client, trainer_headers, class_a, "notes.txt", b"notes", "text/plain")
    document = response.json()["uploaded"][0]["document"]

    preview = client.get(f"/api/documents/{document['id']}/preview", headers=trainer_headers)

    assert preview.status_code == 415
    assert "preview" in preview.json()["detail"].lower()


def test_file_access_is_scoped_to_assigned_classes_and_trainers(
    client, trainer_headers, other_trainer_headers, student_headers
):
    """AI-43 AC-03: the link only works for an authorized trainer."""
    document = _uploaded_pdf(client, trainer_headers)

    assert (
        client.get(f"/api/documents/{document['id']}/download").status_code in (401, 403)
    )
    assert (
        client.get(
            f"/api/documents/{document['id']}/download", headers=student_headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/documents/{document['id']}/download", headers=other_trainer_headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/documents/{uuid.uuid4()}/download", headers=trainer_headers
        ).status_code
        == 404
    )


def test_download_reports_missing_file_as_not_found(db_session, client, trainer_headers):
    """A row whose file vanished on disk must not leak a server path."""
    document = _uploaded_pdf(client, trainer_headers)
    record = db_session.query(models.Document).filter_by(id=uuid.UUID(document["id"])).one()
    Path(record.storage_path).unlink()

    response = client.get(f"/api/documents/{document['id']}/download", headers=trainer_headers)

    assert response.status_code == 404
    assert "file" in response.json()["detail"].lower()
