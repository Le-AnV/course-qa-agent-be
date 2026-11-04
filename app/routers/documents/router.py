"""Documents router: upload / list / approve / delete, scoped by class (AI-35/37/41/39).

Auth (AI-25/27): all endpoints require a Bearer JWT for a trainer or admin
via ``app.core.deps.require_trainer``. Identity is the DB ``User`` row;
class scope resolves from ``course``/``class`` tables.
"""

from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_trainer
from app.models.course import Class
from app.models.document import Document
from app.models.user import User
from app.schemas.documents import (
    AcceptedTypeRead,
    ClassMineRead,
    DocumentDeleteResponse,
    DocumentListResponse,
    DocumentRead,
    DocumentUploadResponse,
    DocumentUploadResult,
)
from app.services import document_service, document_validation_service, ingestion_service
from app.services.document_service import ACCEPTED_EXTENSIONS, VALID_STATUSES

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _require_uploader(record: Document, user: User) -> None:
    if record.uploaded_by != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the uploader can manage this document.",
        )


def _scoped_document(db: Session, document_id: str, user: User) -> Document:
    record = document_service.get_document(db, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if record.class_id not in document_service.my_class_ids(db, user):
        raise HTTPException(status_code=403, detail="Class is not assigned to you.")
    return record


def _stored_file(record: Document) -> Path:
    try:
        path = document_service.document_path(record)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document file not found on server.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file not found on server.")
    return path


def _is_previewable(record: Document) -> bool:
    return record.mime_type == "application/pdf" or record.mime_type.startswith("image/")


def _record_to_read(db: Session, record: Document) -> DocumentRead:
    return DocumentRead(
        id=str(record.id),
        file_name=record.file_name,
        file_type=record.file_type,
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        uploader_id=str(record.uploaded_by),
        class_id=str(record.class_id),
        class_name=document_service.class_name(db, record.class_id),
        status=record.status,
        failure_reason=record.failure_reason,
        version=record.version,
        created_at=record.created_at,
    )


@router.get("/accepted-types", response_model=list[AcceptedTypeRead])
def accepted_types(
    _user: User = Depends(require_trainer),
) -> list[AcceptedTypeRead]:
    """Every accepted file type for the upload screen (Upload AC-07)."""
    labels = {
        ".pdf": "PDF document",
        ".docx": "Word document",
        ".md": "Markdown",
        ".txt": "Plain text",
        ".pptx": "PowerPoint",
        ".xlsx": "Excel",
    }
    return [
        AcceptedTypeRead(extension=ext, mime=mime, label=labels[ext])
        for ext, mime in sorted(ACCEPTED_EXTENSIONS.items())
    ]


@router.get("/classes/mine", response_model=list[ClassMineRead])
def classes_mine(
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> list[ClassMineRead]:
    """Classes assigned to the caller (Upload AC-05 / List AC-05)."""
    return [ClassMineRead(**c) for c in document_service.my_classes(db, user)]


@router.post("/", response_model=DocumentUploadResponse, status_code=201)
async def upload_documents(
    files: list[UploadFile] = File(...),
    class_id: str = Form(...),
    force: bool = Query(default=False),
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentUploadResponse:
    """Multi-file material upload (Upload AC-04); each file validated (AC-06).

    AI-39 AC-03: a repeated file name creates a pending next version; the
    prior version stays active until the replacement is approved and indexed.
    """
    results: list[DocumentUploadResult] = []
    for upload in files:
        content = await upload.read()
        name = upload.filename or "unnamed"
        try:
            record = document_service.create_document(db, name, content, class_id, user)
        except ValueError as exc:
            results.append(DocumentUploadResult(file_name=name, error=str(exc)))
            continue

        if not force:
            clazz = db.get(Class, record.class_id)
            if clazz:
                class_desc = clazz.info.description if getattr(clazz, "info", None) else None
                doc_path = document_service.document_path(record)
                try:
                    loaded = ingestion_service._load_document(doc_path)
                    doc_text = loaded.get("markdown", "")
                except Exception:  # noqa: BLE001
                    doc_text = ""

                is_relevant, rejection_reason = document_validation_service.validate_document_relevance(
                    db,
                    class_name=clazz.name,
                    class_code=clazz.code,
                    class_description=class_desc,
                    file_name=name,
                    document_text=doc_text,
                )
                if not is_relevant:
                    document_service.delete_document(db, record)
                    results.append(
                        DocumentUploadResult(
                            file_name=name,
                            warning=rejection_reason or "Document content does not match course subject.",
                        )
                    )
                    continue

        results.append(DocumentUploadResult(file_name=name, document=_record_to_read(db, record)))
    return DocumentUploadResponse(uploaded=results)


@router.get("/", response_model=DocumentListResponse)
def get_documents(
    search: str = Query(default=""),
    class_id: str | None = Query(default=None),
    file_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    """Browse/search/filter/paginate the caller's own classes (List AC-01..05)."""
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'.")
    rows, total = document_service.list_documents(
        db,
        user,
        search=search,
        class_id=class_id,
        file_type=file_type,
        status=status,
        page=page,
        page_size=page_size,
    )
    return DocumentListResponse(
        items=[_record_to_read(db, r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{document_id}/download")
def download_document(
    document_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> FileResponse:
    """AI-43 AC-02/AC-03: download the original file, trainer scope enforced."""
    record = _scoped_document(db, document_id, user)
    path = _stored_file(record)
    return FileResponse(
        path,
        media_type=record.mime_type or "application/octet-stream",
        filename=record.file_name,
        content_disposition_type="attachment",
    )


@router.get("/{document_id}/preview")
def preview_document(
    document_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> FileResponse:
    """AI-43 AC-01/AC-03: inline preview for PDFs and images only."""
    record = _scoped_document(db, document_id, user)
    if not _is_previewable(record):
        raise HTTPException(
            status_code=415,
            detail="This file type cannot be previewed; download it instead.",
        )
    path = _stored_file(record)
    return FileResponse(
        path,
        media_type=record.mime_type,
        filename=record.file_name,
        content_disposition_type="inline",
    )


@router.post("/{document_id}/approve", response_model=DocumentRead)
def approve_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    force: bool = Query(default=True),
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Approve pending -> processing.

    AI-41 originally said trainer-only (TA gets 403). Under AI-27 RBAC,
    `require_trainer` admits `admin` too (superset); students still get 403
    from the dependency before reaching here.
    """
    record = document_service.get_document(db, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if record.class_id not in document_service.my_class_ids(db, user):
        raise HTTPException(status_code=403, detail="Class is not assigned to you.")
    _require_uploader(record, user)
    try:
        record = document_service.approve_document(db, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    background_tasks.add_task(ingestion_service.process_document_in_background, str(record.id))
    return _record_to_read(db, record)


@router.post("/{document_id}/reject", response_model=DocumentRead)
def reject_document(
    document_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Reject a pending document within the trainer's assigned classes."""
    record = document_service.get_document(db, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if record.class_id not in document_service.my_class_ids(db, user):
        raise HTTPException(status_code=403, detail="Class is not assigned to you.")
    _require_uploader(record, user)
    try:
        record = document_service.reject_document(db, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _record_to_read(db, record)


@router.post("/{document_id}/retry", response_model=DocumentRead)
def retry_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False),
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Retry a failed document through the ingestion pipeline."""
    record = document_service.get_document(db, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if record.class_id not in document_service.my_class_ids(db, user):
        raise HTTPException(status_code=403, detail="Class is not assigned to you.")
    _require_uploader(record, user)
    try:
        record = document_service.retry_document(db, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    background_tasks.add_task(ingestion_service.process_document_in_background, str(record.id))
    return _record_to_read(db, record)


@router.delete("/{document_id}", response_model=DocumentDeleteResponse)
def delete_document(
    document_id: str,
    confirm: bool = Query(default=False),
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentDeleteResponse:
    """Delete a document and purge its chunks from Qdrant (AI-39 AC-01/AC-04).

    Destructive, so it requires ``?confirm=true`` — the UI shows a confirm
    dialog first. Class scope matches approve: another trainer's document is
    never listed (AC-04) and is refused here by id too. When Qdrant cannot be
    reached for a vector-backed file, nothing is deleted and the API answers
    ``503`` so the outdated material can never keep answering (AC-02).
    """
    if not confirm:
        raise HTTPException(
            status_code=400, detail="Deletion requires confirmation (?confirm=true)."
        )
    record = document_service.get_document(db, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if record.class_id not in document_service.my_class_ids(db, user):
        raise HTTPException(status_code=403, detail="Class is not assigned to you.")
    deleted_id, file_name, version = str(record.id), record.file_name, record.version
    try:
        purged = document_service.delete_document(db, record)
    except document_service.VectorPurgeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return DocumentDeleteResponse(
        deleted=deleted_id, file_name=file_name, version=version, purged_chunks=purged
    )
