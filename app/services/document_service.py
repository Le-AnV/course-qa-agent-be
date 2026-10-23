"""Document service for Epic E2 (AI-35/37/41/39): upload, list, tags, delete/replace.

DB-backed (E1 models): class assignments resolve from ``course``/``class``
rows instead of the former static stub, and uploads persist as ``document``
rows with pending status. Original uploads are stored on the persistent
document volume for the AI-55 pipeline to process after approval.

AI-39 adds the delete/replace lifecycle: deleting purges the file's chunks
from Qdrant (delete by ``document_id``), and replacing a file name versions
the row and re-indexes while the superseded version stops answering.
"""

import logging
import uuid
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.course import Class
from app.models.document import Document
from app.models.user import User
from app.services.vector_store.qdrant_store import (
    clone_document_points,
    delete_points_by_document_id,
)

logger = logging.getLogger(__name__)

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


def store_upload_file(
    document_id: uuid.UUID,
    file_name: str,
    content: bytes,
) -> Path:
    """Store the original upload for processing after approval."""
    storage_dir = settings.document_storage_dir.resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)

    extension = Path(file_name).suffix.lower()
    storage_path = storage_dir / f"{document_id}{extension}"
    try:
        storage_path.write_bytes(content)
    except OSError:
        try:
            storage_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove incomplete upload %s", storage_path, exc_info=True)
        raise
    return storage_path


def document_path(record: Document) -> Path:
    """Resolve a source file while keeping access inside the storage root."""
    if not record.storage_path:
        raise FileNotFoundError("Document file is not available.")

    root = settings.document_storage_dir.resolve()
    stored = Path(record.storage_path)
    if stored.is_absolute():
        path = stored.resolve()
    else:
        candidate = stored.resolve()
        path = candidate if candidate.is_relative_to(root) else (root / stored).resolve()
    if not path.is_relative_to(root):
        raise FileNotFoundError("Document file is not available.")
    return path


def copyable_documents(db: Session, class_id: uuid.UUID | str) -> list[Document]:
    """Current approved documents a trainer may carry into another class.

    Only the newest indexed version per file name is offered: replaced,
    rejected, failed and pending rows have no answerable content.
    """
    rows = (
        db.query(Document)
        .filter(
            Document.class_id == uuid.UUID(str(class_id)),
            Document.status == STATUS_INDEXED,
        )
        .order_by(Document.file_name, Document.version.desc())
        .all()
    )
    current: dict[str, Document] = {}
    for row in rows:
        current.setdefault(row.file_name.casefold(), row)
    return sorted(current.values(), key=lambda row: row.file_name.casefold())


def clone_documents_to_class(
    db: Session,
    source_class_id: uuid.UUID | str,
    target_class_id: uuid.UUID | str,
    document_ids: list[uuid.UUID | str],
    user: User,
) -> tuple[list[Document], list[tuple[str, str]]]:
    """Clone approved files and their vectors; never re-approve or re-embed.

    Returns the copied rows and ``(file_name, reason)`` skips. A single
    document that cannot be cloned is skipped so the rest of the template
    still lands; its new file/vectors are cleaned up immediately.
    """
    source_uuid = uuid.UUID(str(source_class_id))
    target_uuid = uuid.UUID(str(target_class_id))
    current = {row.id: row for row in copyable_documents(db, source_uuid)}
    copied: list[Document] = []
    skipped: list[tuple[str, str]] = []

    for raw_id in dict.fromkeys(document_ids):
        try:
            document_id = uuid.UUID(str(raw_id))
        except (TypeError, ValueError, AttributeError):
            skipped.append((str(raw_id), "Unknown document."))
            continue
        source = current.get(document_id)
        if source is None:
            skipped.append((str(raw_id), "Only approved documents can be copied."))
            continue

        existing = (
            db.query(Document)
            .filter(
                Document.class_id == target_uuid,
                func.lower(Document.file_name) == source.file_name.casefold(),
            )
            .first()
        )
        if existing is not None:
            skipped.append(
                (source.file_name, "A document with this name already exists in this class.")
            )
            continue

        new_id = uuid.uuid4()
        target_path: Path | None = None
        try:
            content = document_path(source).read_bytes()
            target_path = store_upload_file(new_id, source.file_name, content)
            points = clone_document_points(str(source.id), str(new_id), str(target_uuid))
            if points == 0:
                raise ValueError("No indexed content is available for this document.")
            record = Document(
                id=new_id,
                class_id=target_uuid,
                uploaded_by=user.id,
                file_name=source.file_name,
                file_type=source.file_type,
                mime_type=source.mime_type,
                size_bytes=len(content),
                storage_path=str(target_path),
                status=STATUS_INDEXED,
                version=1,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            copied.append(record)
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the template
            db.rollback()
            logger.warning("Could not copy document %s: %s", source.id, exc)
            try:
                purge_document_vectors(new_id)
            except Exception:
                logger.warning("Could not purge cloned vectors of %s", new_id, exc_info=True)
            if target_path is not None:
                try:
                    target_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove cloned file %s", target_path, exc_info=True)
            skipped.append((source.file_name, f"Copy failed: {exc}"))

    return copied, skipped


def create_document(
    db: Session, file_name: str, content: bytes, class_id: uuid.UUID | str, user: User
) -> Document:
    """Persist an already-validated upload.

    Upload AC-02: every upload starts ``pending`` for trainer approval.
    AI-39 AC-03: a repeated file name creates the next version. The prior
    version remains available until the approved replacement indexes.
    """
    validated = validate_upload(db, file_name, content, class_id, user)
    if isinstance(validated, str):
        raise ValueError(validated)  # noqa: TRY004 - validation failure, not a type error
    extension, mime, size, class_uuid = validated
    name = file_name.strip() or file_name
    previous = latest_version(db, class_uuid, name)
    document_id = uuid.uuid4()
    stored_path: Path | None = None
    try:
        stored_path = store_upload_file(document_id, name, content)
        record = Document(
            id=document_id,
            class_id=class_uuid,
            uploaded_by=user.id,
            file_name=name,
            file_type=extension,
            mime_type=mime,
            size_bytes=size,
            storage_path=str(stored_path),
            status=STATUS_PENDING,
            version=1 if previous is None else previous.version + 1,
        )
        db.add(record)
        db.commit()
    except Exception:
        db.rollback()
        if stored_path is not None:
            try:
                stored_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Could not remove uncommitted upload %s", stored_path, exc_info=True
                )
        raise
    db.refresh(record)
    return record


def supersede_older_versions(db: Session, record: Document) -> int:
    """Activate an indexed version and retire older versions in one DB update."""
    older = (
        db.query(Document)
        .filter(
            Document.class_id == record.class_id,
            func.lower(Document.file_name) == record.file_name.strip().lower(),
            Document.version < record.version,
            Document.status != STATUS_REPLACED,
        )
        .all()
    )
    newer_indexed = (
        db.query(Document.id)
        .filter(
            Document.class_id == record.class_id,
            func.lower(Document.file_name) == record.file_name.strip().lower(),
            Document.version > record.version,
            Document.status == STATUS_INDEXED,
        )
        .first()
    )
    if newer_indexed:
        purged = purge_document_vectors(record.id)
        record.status = STATUS_REPLACED
        record.failure_reason = None
        db.commit()
        try:
            document_path(record).unlink(missing_ok=True)
        except (FileNotFoundError, OSError):
            logger.warning("Could not remove stale source file for document %s", record.id)
        return purged

    purged = 0
    for previous in older:
        if previous.status in VECTOR_BACKED_STATUSES:
            purged += purge_document_vectors(previous.id)
    paths: list[Path] = []
    for previous in older:
        if previous.storage_path:
            try:
                paths.append(document_path(previous))
            except FileNotFoundError:
                pass
        previous.status = STATUS_REPLACED
    record.status = STATUS_INDEXED
    record.failure_reason = None
    db.commit()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove superseded source file %s", path, exc_info=True)
    return purged


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
    record.failure_reason = None
    db.commit()
    db.refresh(record)
    return record


def reject_document(db: Session, document_id: uuid.UUID | str) -> Document:
    """Trainer rejection: pending -> rejected."""
    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    if record.status != STATUS_PENDING:
        raise ValueError(f"Only pending documents can be rejected (is {record.status}).")
    record.status = STATUS_REJECTED
    db.commit()
    db.refresh(record)
    return record


def retry_document(db: Session, document_id: uuid.UUID | str) -> Document:
    """Trainer retry: failed -> processing."""
    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    if record.status != STATUS_FAILED:
        raise ValueError(f"Only failed documents can be retried (is {record.status}).")
    record.status = STATUS_PROCESSING
    record.failure_reason = None
    db.commit()
    db.refresh(record)
    return record


def fail_stale_processing(db: Session) -> int:
    """Mark interrupted processing jobs failed after a single-instance restart."""
    records = db.query(Document).filter(Document.status == STATUS_PROCESSING).all()
    for record in records:
        record.status = STATUS_FAILED
        record.failure_reason = (
            "Processing was interrupted by a server restart. Retry the document."
        )
    if records:
        db.commit()
    return len(records)


def delete_document(db: Session, record: Document) -> int:
    """Delete one document and its chunks (AI-39 AC-01).

    Vector-backed files are purged first: when Qdrant is unreachable the row
    is kept and ``VectorPurgeError`` propagates, so a trainer never gets a
    "deleted" answer while the chatbot can still cite the file (AC-02).
    Returns the number of purged points.
    """
    try:
        source_path = document_path(record)
    except FileNotFoundError:
        source_path = None

    purged = 0
    if record.status in VECTOR_BACKED_STATUSES:
        purged = purge_document_vectors(record.id)
    db.delete(record)
    db.commit()
    if source_path is not None:
        try:
            source_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove stored file %s", source_path, exc_info=True)
    return purged


def retrievable_document_ids(db: Session, class_id: uuid.UUID | str) -> set[str]:
    """Ids inside one class that may still answer questions (AI-39 AC-02).

    Only successfully indexed documents may answer questions. Deleted rows
    are absent; replaced, rejected, failed, pending, and processing rows are
    excluded.
    """
    try:
        class_uuid = uuid.UUID(str(class_id))
    except (TypeError, ValueError, AttributeError):
        return set()
    rows = (
        db.query(Document.id)
        .filter(
            Document.class_id == class_uuid,
            Document.status == STATUS_INDEXED,
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
