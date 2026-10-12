"""Documents router: upload / list, scoped by class (AI-35/37)."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from app.dependencies.auth import require_trainer
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


def _record_to_read(record: document_service.DocumentRecord) -> DocumentRead:
    return DocumentRead(
        id=record.id,
        file_name=record.file_name,
        file_type=record.file_type,
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        uploader_id=record.uploader_id,
        class_id=record.class_id,
        class_name=document_service.CLASSES.get(record.class_id, {}).get(
            "name", record.class_id
        ),
        status=record.status,
        created_at=record.created_at,
    )


@router.get("/accepted-types", response_model=list[AcceptedTypeRead])
def accepted_types(
    _user: dict = Depends(require_trainer),  # noqa: B008 - FastAPI Depends pattern
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
    user: dict = Depends(require_trainer),  # noqa: B008
) -> list[ClassMineRead]:
    """Classes assigned to the caller (Upload AC-05 / List AC-05)."""
    return [ClassMineRead(**c) for c in document_service.my_classes(user["identifier"])]


@router.post("/", response_model=DocumentUploadResponse, status_code=201)
async def upload_documents(
    files: list[UploadFile] = File(...),  # noqa: B008 - canonical FastAPI pattern
    class_id: str = Form(...),
    user: dict = Depends(require_trainer),  # noqa: B008
) -> DocumentUploadResponse:
    """Multi-file material upload (Upload AC-04); each file validated (AC-06)."""
    user_id = user["identifier"]
    results: list[DocumentUploadResult] = []
    for upload in files:
        content = await upload.read()
        name = upload.filename or "unnamed"
        try:
            record = document_service.create_document(name, content, class_id, user_id)
        except ValueError as exc:
            results.append(DocumentUploadResult(file_name=name, error=str(exc)))
            continue
        results.append(
            DocumentUploadResult(file_name=name, document=_record_to_read(record))
        )
    return DocumentUploadResponse(uploaded=results)


@router.get("/", response_model=DocumentListResponse)
def get_documents(
    search: str = Query(default=""),
    class_id: str | None = Query(default=None),
    file_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(require_trainer),  # noqa: B008
) -> DocumentListResponse:
    """Browse/search/filter/paginate the caller's own classes (List AC-01..05)."""
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'.")
    rows, total = document_service.list_documents(
        user_id=user["identifier"],
        search=search,
        class_id=class_id,
        file_type=file_type,
        status=status,
        page=page,
        page_size=page_size,
    )
    return DocumentListResponse(
        items=[_record_to_read(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
