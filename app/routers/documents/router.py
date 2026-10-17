"""Documents router: upload / list / approve, scoped by class (AI-35/37/41).

Auth (AI-25/27): all endpoints require a Bearer JWT for a trainer or admin
via ``app.core.deps.require_trainer``. Identity is the DB ``User`` row;
class scope resolves from ``course``/``class`` tables.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db, require_trainer
from app.models.document import Document
from app.models.user import User
from app.schemas.documents import (
    AcceptedTypeRead,
    ClassMineRead,
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
        ".csv": "CSV",
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
    """Multi-file material upload (Upload AC-04); each file validated (AC-06)."""
    results: list[DocumentUploadResult] = []
    for upload in files:
        content = await upload.read()
        name = upload.filename or "unnamed"
        try:
            record = document_service.create_document(db, name, content, class_id, user)
        except ValueError as exc:
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


@router.get("/{document_id}/content")
def read_document_content(
    document_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Open an original document after checking class membership."""
    record = document_service.get_document(db, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if not document_service.can_access_class(db, user, record.class_id):
        raise HTTPException(status_code=403, detail="You do not have access to this document.")
    try:
        path = document_service.document_path(record)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document file is not available.") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file is not available.")
    return FileResponse(
        path,
        media_type=record.mime_type,
        filename=record.file_name,
        content_disposition_type="inline",
    )
