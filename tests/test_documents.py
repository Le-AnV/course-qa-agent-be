"""Tests for Epic E2: upload documents (AI-35).

Auth (AI-25/27): endpoints require `Authorization: Bearer <JWT>`.
Tokens are minted with app.services.auth.create_access_token so tests track
the real RBAC (trainer+admin allowed, student denied, missing token = 401).
"""

import io
import zipfile

from fastapi.testclient import TestClient

from app.main import app
from app.services import document_service
from app.services.auth import create_access_token

client = TestClient(app)

TRAINER_ID = "teacher@academic.edu.vn"
ADMIN_ID = "admin@academic.edu.vn"
STUDENT_ID = "student@academic.edu.vn"


def _auth_headers(identifier: str, role: str) -> dict[str, str]:
    token = create_access_token({"sub": identifier, "role": role})
    return {"Authorization": f"Bearer {token}"}


TRAINER = _auth_headers(TRAINER_ID, "trainer")
ADMIN = _auth_headers(ADMIN_ID, "admin")
STUDENT = _auth_headers(STUDENT_ID, "student")


def _setup() -> None:
    document_service.reset_store()


def _zip_bytes(inner: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<types/>")
        zf.writestr(inner, "x")
    return buf.getvalue()


def test_accepted_types_lists_seven_material_formats():
    _setup()
    response = client.get("/api/documents/accepted-types", headers=TRAINER)
    assert response.status_code == 200
    extensions = {t["extension"] for t in response.json()}
    assert extensions == {".pdf", ".docx", ".md", ".txt", ".pptx", ".xlsx", ".csv"}


def test_accepted_types_requires_auth():
    _setup()
    assert client.get("/api/documents/accepted-types").status_code in (401, 403)
    assert client.get("/api/documents/accepted-types", headers=STUDENT).status_code == 403


def test_upload_pdf_is_pending_approval():
    _setup()
    files = [("files", ("syllabus.pdf", b"%PDF-1.4 content", "application/pdf"))]
    response = client.post("/api/documents/", files=files,
                           data={"class_id": "class-a"}, headers=TRAINER)
    assert response.status_code == 201
    result = response.json()["uploaded"][0]
    assert result["error"] is None
    assert result["document"]["status"] == "pending"
    assert result["document"]["class_id"] == "class-a"


def test_upload_requires_trainer_role():
    _setup()
    files = [("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf"))]
    assert client.post("/api/documents/", files=files,
                       data={"class_id": "class-a"}).status_code in (401, 403)
    forbidden = client.post("/api/documents/", files=files,
                            data={"class_id": "class-a"}, headers=STUDENT)
    assert forbidden.status_code == 403


def test_multi_file_upload_reports_per_file_errors():
    _setup()
    files = [
        ("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf")),
        ("files", ("virus.exe", b"MZ binary", "application/octet-stream")),
        ("files", ("notes.txt", b"hello", "text/plain")),
    ]
    response = client.post("/api/documents/", files=files,
                           data={"class_id": "class-a"}, headers=TRAINER)
    assert response.status_code == 201
    uploaded = response.json()["uploaded"]
    assert uploaded[0]["error"] is None
    assert "Invalid file format" in uploaded[1]["error"]
    assert uploaded[2]["error"] is None


def test_renamed_exe_to_pdf_is_rejected():
    _setup()
    files = [("files", ("evil.pdf", b"MZ\x90\x00 binary", "application/pdf"))]
    response = client.post("/api/documents/", files=files,
                           data={"class_id": "class-a"}, headers=TRAINER)
    assert response.status_code == 201
    assert "not a real PDF" in response.json()["uploaded"][0]["error"]


def test_renamed_txt_to_docx_is_rejected():
    _setup()
    files = [("files", ("fake.docx", b"plain text, not zip", "application/vnd.openxmlformats"))]
    response = client.post("/api/documents/", files=files,
                           data={"class_id": "class-a"}, headers=TRAINER)
    assert response.status_code == 201
    assert "not a real .docx" in response.json()["uploaded"][0]["error"]


def test_oversize_upload_is_rejected():
    _setup()
    old = document_service.MAX_FILE_SIZE_BYTES
    document_service.MAX_FILE_SIZE_BYTES = 10
    try:
        error = document_service.validate_upload("big.pdf", b"%PDF-" + b"x" * 20,
                                                 "class-a", TRAINER_ID)
    finally:
        document_service.MAX_FILE_SIZE_BYTES = old
    assert isinstance(error, str) and "100MB" in error


def test_classes_mine_only_returns_assigned():
    _setup()
    assert {c["id"] for c in client.get("/api/documents/classes/mine",
                                       headers=TRAINER).json()} == {"class-a", "class-b"}
    # Admin shares the trainer class scope under the provisional in-memory map.
    assert {c["id"] for c in client.get("/api/documents/classes/mine",
                                       headers=ADMIN).json()} == {"class-a", "class-b"}
    assert client.get("/api/documents/classes/mine", headers=STUDENT).status_code == 403


def test_list_search_filter_pagination_and_scope():
    _setup()
    document_service.create_document("algebra.pdf", b"%PDF-a", "class-a", TRAINER_ID)
    document_service.create_document("algebra-notes.txt", b"notes", "class-a", TRAINER_ID)
    document_service.create_document("physics.pdf", b"%PDF-p", "class-b", TRAINER_ID)

    # Service-level scope: single-class user only sees class-a (List AC-05).
    _, total = document_service.list_documents(user_id="ta-1")
    assert total == 2

    search = client.get("/api/documents/", params={"search": "algebra"},
                        headers=TRAINER).json()
    assert search["total"] == 2

    filtered = client.get("/api/documents/",
                          params={"file_type": ".pdf", "status": "pending"},
                          headers=TRAINER).json()
    assert filtered["total"] == 2
    assert all(i["status"] == "pending" for i in filtered["items"])

    page1 = client.get("/api/documents/", params={"page": 1, "page_size": 2},
                       headers=TRAINER).json()
    page2 = client.get("/api/documents/", params={"page": 2, "page_size": 2},
                       headers=TRAINER).json()
    assert page1["total"] == 3 and len(page1["items"]) == 2 and len(page2["items"]) == 1

    row = page1["items"][0]
    assert {"file_name", "file_type", "size_bytes", "uploader_id",
            "class_id", "created_at"} <= set(row)

    assert client.get("/api/documents/", headers=STUDENT).status_code == 403
