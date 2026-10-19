"""Documents router: upload / list / approve / delete, scoped by class (AI-35/37/41/39).

Auth (AI-25/27): all endpoints require a Bearer JWT for a trainer or admin
via ``app.core.deps.require_trainer``. Identity is the DB ``User`` row;
class scope resolves from ``course``/``class`` tables.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_trainer
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
from app.services import document_service
from app.services.document_service import ACCEPTED_EXTENSIONS, VALID_STATUSES

router = APIRouter(prefix="/api/documents", tags=["documents"])


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
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentUploadResponse:
    """Multi-file material upload (Upload AC-04); each file validated (AC-06).

    AI-39 AC-03: a file name already used inside the class becomes the next
    version (handed straight to re-indexing) and supersedes the old one.
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
        except document_service.VectorPurgeError as exc:
            # Replace accepted only when the outdated chunks are gone (AC-02).
            results.append(DocumentUploadResult(file_name=name, error=str(exc)))
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


@router.post("/{document_id}/approve", response_model=DocumentRead)
def approve_document(
    document_id: str,
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
    try:
        record = document_service.approve_document(db, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
