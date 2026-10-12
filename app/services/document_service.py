"""Document service for Epic E2 (AI-35/37/41): upload validation, list, class tags.

Provisional storage: in-memory dicts stand in for the future
`courses/classes/class_assignments/documents` tables (AI-119/AI-31).
Routers call these functions; this module never imports routers.
"""

import csv
import io
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Upload AC-01: 7 accepted material formats (images belong to the E3 vision path).
ACCEPTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
}

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # Upload AC-01: 100MB per file.

STATUS_PENDING = "pending"  # Displayed as "pending-approval" (Upload AC-02).
STATUS_PROCESSING = "processing"
STATUS_INDEXED = "indexed"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
VALID_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXED, STATUS_REJECTED, STATUS_FAILED}
)

# Stub directory: replaced by E1 (AI-31 classes + AI-27 JWT) without API changes.
# Keys are JWT `sub` identifiers (see app/services/auth.py MOCK_USERS).
# Legacy stub ids ("trainer-1"/"ta-1") kept so service unit tests still pass.
CLASSES: dict[str, dict[str, str]] = {
    "class-a": {"id": "class-a", "course_id": "course-cs101",
                "name": "CS101 - Class A", "code": "CS101-A"},
    "class-b": {"id": "class-b", "course_id": "course-cs101",
                "name": "CS101 - Class B", "code": "CS101-B"},
}
CLASS_ASSIGNMENTS: dict[str, list[str]] = {
    "teacher@academic.edu.vn": ["class-a", "class-b"],
    "admin@academic.edu.vn": ["class-a", "class-b"],
    "trainer-1": ["class-a", "class-b"],
    "ta-1": ["class-a"],
}


@dataclass
class DocumentRecord:
    id: str
    file_name: str
    file_type: str
    mime_type: str
    size_bytes: int
    uploader_id: str
    class_id: str
    status: str = STATUS_PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# Module-level store (tests reset via `reset_store`).
_DOCUMENTS: dict[str, DocumentRecord] = {}


def reset_store() -> None:
    _DOCUMENTS.clear()


def my_class_ids(user_id: str) -> list[str]:
    """Classes assigned to the caller (Upload AC-05 / List AC-05)."""
    return list(CLASS_ASSIGNMENTS.get(user_id, []))


def detect_content_kind(extension: str, content: bytes) -> str | None:
    """Return an error message when bytes do not match the extension.

    Returns None when content is consistent (Upload AC-06, stdlib only).
    """
    if extension == ".pdf":
        if not content.startswith(b"%PDF"):
            return "content is not a real PDF file"
        return None
    if extension in (".docx", ".pptx", ".xlsx"):
        if not content.startswith(b"PK\x03\x04"):
            return f"content is not a real {extension} (ZIP-based) file"
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile:
            return f"content is not a real {extension} file"
        if "[Content_Types].xml" not in names:
            return f"content is not a real {extension} file"
        expected = {".docx": "word/", ".pptx": "ppt/", ".xlsx": "xl/"}[extension]
        if not any(n.startswith(expected) for n in names):
            return f"content is not a real {extension} file"
        return None
    if extension in (".md", ".txt", ".csv"):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return f"content is not a real {extension} (UTF-8 text) file"
        if "\x00" in text:
            return f"content is not a real {extension} (UTF-8 text) file"
        if extension == ".csv":
            try:
                list(csv.reader(io.StringIO(text)))
            except csv.Error:
                return "content is not a real CSV file"
        return None
    return f"unsupported file format: {extension}"


def validate_upload(
    file_name: str, content: bytes, class_id: str, user_id: str
) -> tuple[str, str, int] | str:
    """Validate one file. Returns (extension, mime, size) or an error message."""
    lowered = file_name.lower()
    dot = lowered.rfind(".")
    extension = lowered[dot:] if dot != -1 else ""
    if extension not in ACCEPTED_EXTENSIONS:
        accepted = ", ".join(sorted(ACCEPTED_EXTENSIONS))
        return f"Invalid file format '{extension or file_name}'. Accepted: {accepted}."
    size = len(content)
    if size > MAX_FILE_SIZE_BYTES:
        return f"File '{file_name}' exceeds 100MB per file."
    if class_id not in CLASSES:
        return f"Class '{class_id}' does not exist (admin creates classes)."
    if class_id not in my_class_ids(user_id):
        return "Class is not assigned to you."
    mismatch = detect_content_kind(extension, content)
    if mismatch is not None:
        return f"File '{file_name}' rejected: {mismatch}."
    return extension, ACCEPTED_EXTENSIONS[extension], size


def create_document(
    file_name: str, content: bytes, class_id: str, user_id: str
) -> DocumentRecord:
    """Persist an already-validated upload with pending status (Upload AC-02)."""
    validated = validate_upload(file_name, content, class_id, user_id)
    if isinstance(validated, str):
        raise ValueError(validated)  # noqa: TRY004 - validation failure, not a type error
    extension, mime, size = validated
    record = DocumentRecord(
        id=str(uuid.uuid4()),
        file_name=file_name,
        file_type=extension,
        mime_type=mime,
        size_bytes=size,
        uploader_id=user_id,
        class_id=class_id,
    )
    _DOCUMENTS[record.id] = record
    return record


def get_document(document_id: str) -> DocumentRecord | None:
    return _DOCUMENTS.get(document_id)
