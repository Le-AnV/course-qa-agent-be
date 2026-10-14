"""Document service for Epic E2 (AI-35/37/41): upload validation, list, class tags.

DB-backed (E1 models): class assignments resolve from ``course``/``class``
rows instead of the former static stub, and uploads persist as ``document``
rows with pending status. File bytes are validated then discarded — blob
storage lands with the AI-55 indexing pipeline.
"""

import uuid

from sqlalchemy.orm import Session

from app.models.course import Class, Course
from app.models.document import Document
from app.models.user import User

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


def reset_store() -> None:
    """Deprecated no-op: storage is the database now (kept for old callers)."""


def _role_name(user: User) -> str:
    role = getattr(user, "role", None)
    if role is not None and getattr(role, "name", None):
        return role.name
    return ""


def my_class_ids(db: Session, user: User) -> list[uuid.UUID]:
    """Class ids visible to the caller (Upload AC-05 / List AC-05)."""
    role = _role_name(user).upper()
    if role == "ADMIN":
        return [c.id for c in db.query(Class.id).all()]
    if role == "TRAINER":
        return [c.id for c in db.query(Class.id).filter(Class.trainer_id == user.id).all()]
    return []


def my_classes(db: Session, user: User) -> list[dict]:
    ids = set(my_class_ids(db, user))
    if not ids:
        return []
    rows = db.query(Class).filter(Class.id.in_(ids)).order_by(Class.code).all()
    return [
        {"id": str(c.id), "course_id": str(c.course_id), "name": c.name, "code": c.code}
        for c in rows
    ]


def detect_content_kind(extension: str, content: bytes) -> str | None:
    """Return an error message when bytes do not match the extension.

    Returns None when content is consistent (Upload AC-06, stdlib only).
    """
    import csv
    import io
    import zipfile

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
    db: Session, file_name: str, content: bytes, class_id: uuid.UUID | str, user: User
) -> tuple[str, str, int, uuid.UUID] | str:
    """Validate one file. Returns (extension, mime, size, class_uuid) or an error."""
    lowered = file_name.lower()
    dot = lowered.rfind(".")
    extension = lowered[dot:] if dot != -1 else ""
    if extension not in ACCEPTED_EXTENSIONS:
        accepted = ", ".join(sorted(ACCEPTED_EXTENSIONS))
        return f"Invalid file format '{extension or file_name}'. Accepted: {accepted}."
    size = len(content)
    if size > MAX_FILE_SIZE_BYTES:
        return f"File '{file_name}' exceeds 100MB per file."
    try:
        class_uuid = uuid.UUID(str(class_id))
    except (TypeError, ValueError, AttributeError):
        return f"Class '{class_id}' does not exist (admin creates classes)."
    clazz = db.query(Class).filter(Class.id == class_uuid).first()
    if clazz is None:
        return f"Class '{class_id}' does not exist (admin creates classes)."
    if class_uuid not in set(my_class_ids(db, user)):
        return "Class is not assigned to you."
    mismatch = detect_content_kind(extension, content)
    if mismatch is not None:
        return f"File '{file_name}' rejected: {mismatch}."
    return extension, ACCEPTED_EXTENSIONS[extension], size, class_uuid


def create_document(
    db: Session, file_name: str, content: bytes, class_id: uuid.UUID | str, user: User
) -> Document:
    """Persist an already-validated upload with pending status (Upload AC-02)."""
    validated = validate_upload(db, file_name, content, class_id, user)
    if isinstance(validated, str):
        raise ValueError(validated)  # noqa: TRY004 - validation failure, not a type error
    extension, mime, size, class_uuid = validated
    record = Document(
        class_id=class_uuid,
        uploaded_by=user.id,
        file_name=file_name,
        file_type=extension,
        mime_type=mime,
        size_bytes=size,
        status=STATUS_PENDING,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_document(db: Session, document_id: uuid.UUID | str) -> Document | None:
    try:
        doc_uuid = uuid.UUID(str(document_id))
    except (TypeError, ValueError, AttributeError):
        return None
    return db.query(Document).filter(Document.id == doc_uuid).first()


def approve_document(db: Session, document_id: uuid.UUID | str) -> Document:
    """Trainer approval: pending -> processing (AI-55 pipeline takes over)."""
    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    if record.status != STATUS_PENDING:
        raise ValueError(f"Only pending documents can be approved (is {record.status}).")
    record.status = STATUS_PROCESSING
    db.commit()
    db.refresh(record)
    return record


def list_documents(
    db: Session,
    user: User,
    search: str = "",
    class_id: uuid.UUID | str | None = None,
    file_type: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Document], int]:
    """Search/filter/paginate within the caller's own classes (List AC-02..05)."""
    allowed = set(my_class_ids(db, user))
    query = (
        db.query(Document).filter(Document.class_id.in_(allowed))
        if allowed
        else db.query(Document).filter(False)
    )
    needle = search.strip().lower()
    if needle:
        query = query.filter(Document.file_name.ilike(f"%{needle}%"))
    if class_id is not None:
        try:
            query = query.filter(Document.class_id == uuid.UUID(str(class_id)))
        except (TypeError, ValueError, AttributeError):
            return [], 0
    if file_type is not None:
        query = query.filter(Document.file_type == file_type)
    if status is not None:
        query = query.filter(Document.status == status)
    total = query.count()
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    rows = (
        query.order_by(Document.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, total


def build_qdrant_payload(record: Document) -> dict[str, str]:
    """Native payload filter for class-scoped RAG (Tag AC-03/04)."""
    return {
        "document_id": str(record.id),
        "class_id": str(record.class_id),
        "file_name": record.file_name,
        "file_type": record.file_type,
    }


def class_name(db: Session, class_id: uuid.UUID | str) -> str:
    try:
        clazz = db.query(Class).filter(Class.id == uuid.UUID(str(class_id))).first()
    except (TypeError, ValueError, AttributeError):
        return str(class_id)
    if clazz is None:
        return str(class_id)
    course = db.query(Course).filter(Course.id == clazz.course_id).first()
    _ = course
    return clazz.name
