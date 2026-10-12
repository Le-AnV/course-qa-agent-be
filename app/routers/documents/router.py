"""Documents router: upload materials, scoped by class (AI-35)."""

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.dependencies.auth import require_trainer
from app.schemas.documents import (
    AcceptedTypeRead,
    DocumentRead,
    DocumentUploadResponse,
    DocumentUploadResult,
)
from app.services import document_service
from app.services.document_service import ACCEPTED_EXTENSIONS

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
