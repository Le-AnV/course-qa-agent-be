"""Document service for Epic E2 (AI-35/37/41/39): upload, list, tags, delete/replace.

DB-backed (E1 models): class assignments resolve from ``course``/``class``
rows instead of the former static stub, and uploads persist as ``document``
rows with pending status. File bytes are validated then discarded — blob
storage lands with the AI-55 indexing pipeline.

AI-39 adds the delete/replace lifecycle: deleting purges the file's chunks
from Qdrant (delete by ``document_id``), and replacing a file name versions
the row and re-indexes while the superseded version stops answering.
"""

import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.course import Class
from app.models.document import Document
from app.models.user import User
from app.services.vector_store.qdrant_store import delete_points_by_document_id

# Upload AC-01: 6 accepted material formats (images belong to the E3 vision path).
ACCEPTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # Upload AC-01: 100MB per file.

STATUS_PENDING = "pending"  # Displayed as "pending-approval" (Upload AC-02).
STATUS_PROCESSING = "processing"
STATUS_INDEXED = "indexed"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
# AI-39 AC-03: a newer version of the same file name supersedes this row.
STATUS_REPLACED = "replaced"
VALID_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_PROCESSING,
        STATUS_INDEXED,
        STATUS_REJECTED,
        STATUS_FAILED,
        STATUS_REPLACED,
    }
)

# Statuses whose chunks can exist in Qdrant; delete/replace must purge them.
VECTOR_BACKED_STATUSES = frozenset({STATUS_PROCESSING, STATUS_INDEXED})


class VectorPurgeError(RuntimeError):
    """Qdrant could not be reached, so stale chunks cannot be removed safely."""


def reset_store() -> None:
    """Deprecated no-op: storage is the database now (kept for old callers)."""


def _role_name(user: User) -> str:
    role = getattr(user, "role", None)
    if role is not None and getattr(role, "name", None):
        return role.name
    return ""


def my_class_ids(db: Session, user: User) -> list[uuid.UUID]:
    """Class ids visible to the caller (Upload AC-05 / List AC-05).

    Archived classes leave every workspace surface, so they are excluded.
    """
    role = _role_name(user).upper()
    if role == "ADMIN":
        return [c.id for c in db.query(Class.id).all()]
    if role == "TRAINER":
        from app.services.course_service import class_ids_for_trainer

        return sorted(class_ids_for_trainer(db, user.id, active_only=True), key=str)
    return []


def my_classes(db: Session, user: User) -> list[dict]:
    ids = set(my_class_ids(db, user))
    if not ids:
        return []
    rows = db.query(Class).filter(Class.id.in_(ids)).order_by(Class.code).all()
    return [
        {"id": str(c.id), "name": c.name, "code": c.code}
        for c in rows
    ]


def detect_content_kind(extension: str, content: bytes) -> str | None:
    """Return an error message when bytes do not match the extension.

    Returns None when content is consistent (Upload AC-06, stdlib only).
    """
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
    if extension in (".md", ".txt"):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return f"content is not a real {extension} (UTF-8 text) file"
        if "\x00" in text:
            return f"content is not a real {extension} (UTF-8 text) file"
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


def latest_version(db: Session, class_id: uuid.UUID | str, file_name: str) -> Document | None:
    """Newest row for one file name inside a class (AI-39 AC-03).

    File names match case-insensitively, like the list search, so
    ``Syllabus.pdf`` replaces ``syllabus.pdf``.
    """
    try:
        class_uuid = uuid.UUID(str(class_id))
    except (TypeError, ValueError, AttributeError):
        return None
    needle = file_name.strip().lower()
    if not needle:
        return None
    return (
        db.query(Document)
        .filter(
            Document.class_id == class_uuid,
            func.lower(Document.file_name) == needle,
        )
        .order_by(Document.version.desc(), Document.created_at.desc())
        .first()
    )


def purge_document_vectors(document_id: uuid.UUID | str) -> int:
    """Delete every chunk of one document from Qdrant (AI-39 AC-01/AC-03).

    Returns the number of removed points (0 when the file was never indexed).
    Raises ``VectorPurgeError`` when the vector store is unreachable so the
    caller can refuse a delete/replace that would leave stale chunks behind
    and keep answering student questions.
    """
    try:
        return delete_points_by_document_id(str(document_id))
    except VectorPurgeError:
        raise
    except Exception as exc:
        raise VectorPurgeError(f"Cannot purge chunks of {document_id}: {exc}") from exc


def create_document(
    db: Session, file_name: str, content: bytes, class_id: uuid.UUID | str, user: User
) -> Document:
    """Persist an already-validated upload.

    Upload AC-02: a brand-new file starts ``pending`` (trainer approval).
    AI-39 AC-03: a file name that already exists in the class is a
    replacement — it becomes the next version, is handed straight to the
    re-index pipeline (``processing``, no second approval), and the
    superseded version is marked ``replaced`` with its chunks purged so the
    chatbot stops answering from the outdated material.
    """
    validated = validate_upload(db, file_name, content, class_id, user)
    if isinstance(validated, str):
        raise ValueError(validated)  # noqa: TRY004 - validation failure, not a type error
    extension, mime, size, class_uuid = validated
    name = file_name.strip() or file_name
    previous = latest_version(db, class_uuid, name)
    record = Document(
        class_id=class_uuid,
        uploaded_by=user.id,
        file_name=name,
        file_type=extension,
        mime_type=mime,
        size_bytes=size,
        status=STATUS_PENDING if previous is None else STATUS_PROCESSING,
        version=1 if previous is None else previous.version + 1,
    )
    db.add(record)
    if previous is not None:
        # Purge before commit: a failed purge aborts the whole replacement
        # instead of leaving the old version answering questions.
        try:
            if previous.status in VECTOR_BACKED_STATUSES:
                purge_document_vectors(previous.id)
        except VectorPurgeError:
            db.rollback()  # drop the not-yet-committed new version
            raise
        previous.status = STATUS_REPLACED
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


def delete_document(db: Session, record: Document) -> int:
    """Delete one document and its chunks (AI-39 AC-01).

    Vector-backed files are purged first: when Qdrant is unreachable the row
    is kept and ``VectorPurgeError`` propagates, so a trainer never gets a
    "deleted" answer while the chatbot can still cite the file (AC-02).
    Returns the number of purged points.
    """
    purged = 0
    if record.status in VECTOR_BACKED_STATUSES:
        purged = purge_document_vectors(record.id)
    db.delete(record)
    db.commit()
    return purged


def retrievable_document_ids(db: Session, class_id: uuid.UUID | str) -> set[str]:
    """Ids inside one class that may still answer questions (AI-39 AC-02).

    A document is retrievable while the row exists — deleted rows are gone
    from the DB — and has not been superseded (``replaced``) or rejected.
    """
    try:
        class_uuid = uuid.UUID(str(class_id))
    except (TypeError, ValueError, AttributeError):
        return set()
    rows = (
        db.query(Document.id)
        .filter(
            Document.class_id == class_uuid,
            Document.status.in_((STATUS_PROCESSING, STATUS_INDEXED)),
        )
        .all()
    )
    return {str(row[0]) for row in rows}


def filter_retrievable_chunks(db: Session, class_id: uuid.UUID | str, chunks: list) -> list:
    """Drop retrieved chunks that no longer belong to a live document.

    AI-39 AC-02: E4 RAG applies this after the Qdrant search and before
    answer generation, so a deleted or replaced file can never be cited even
    if its vectors were not fully purged yet. Chunks must carry
    ``document_id`` metadata (``build_qdrant_payload``); unverifiable chunks
    are dropped.
    """
    allowed = retrievable_document_ids(db, class_id)
    kept = []
    for chunk in chunks:
        metadata = getattr(chunk, "metadata", None) or {}
        document_id = str(metadata.get("document_id", ""))
        if document_id and document_id in allowed:
            kept.append(chunk)
    return kept


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
    return clazz.name
