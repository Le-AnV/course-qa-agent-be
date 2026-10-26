"""Class-scoped SSE chat endpoint (AI-61)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.course import Class, ClassTrainer, Enrollment
from app.models.document import Document
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services import (
    chat_context_service,
    chat_service,
    document_service,
    pdf_highlight_service,
)
from app.services.course_service import STATUS_ARCHIVED

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _require_class_access(db: Session, user: User, class_id: uuid.UUID) -> None:
    """Ensure the authenticated user can access the requested class."""

    clazz = db.query(Class).filter(Class.id == class_id).first()
    if clazz is None:
        raise HTTPException(status_code=404, detail="Class not found.")

    role_name = user.role.name
    if role_name != "ADMIN" and clazz.status == STATUS_ARCHIVED:
        raise HTTPException(status_code=404, detail="Class not found.")

    if role_name == "ADMIN":
        return

    if role_name == "TRAINER":
        is_primary_trainer = clazz.trainer_id == user.id
        is_co_trainer = (
            db.query(ClassTrainer)
            .filter(
                ClassTrainer.class_id == class_id,
                ClassTrainer.trainer_id == user.id,
            )
            .first()
            is not None
        )
        if is_primary_trainer or is_co_trainer:
            return
    else:
        enrollment = (
            db.query(Enrollment)
            .filter(
                Enrollment.class_id == class_id,
                Enrollment.student_id == user.id,
                Enrollment.status == "ACTIVE",
            )
            .first()
        )
        if enrollment is not None:
            return

    raise HTTPException(status_code=403, detail="You are not a member of this class.")


@router.get("/sources/{document_id}")
def open_source(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Open a cited source after enforcing its class access boundary."""

    record = db.query(Document).filter(Document.id == document_id).first()
    if record is None or record.status != document_service.STATUS_INDEXED:
        raise HTTPException(status_code=404, detail="Document not found.")

    _require_class_access(db=db, user=current_user, class_id=record.class_id)
    try:
        path = document_service.document_path(record)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document file is not available.") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file is not available.")

    return FileResponse(
        path=path,
        media_type=record.mime_type,
        filename=record.file_name,
        content_disposition_type="inline",
    )


class HighlightRequest(BaseModel):
    """Passages to mark on one page of a cited PDF."""

    page: int = Field(ge=1)
    passages: list[str] = Field(default_factory=list, max_length=20)


@router.post("/sources/{document_id}/highlight")
def highlight_source(
    document_id: uuid.UUID,
    payload: HighlightRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Return the cited PDF with the passages used for the answer highlighted."""

    record = db.query(Document).filter(Document.id == document_id).first()
    if record is None or record.status != document_service.STATUS_INDEXED:
        raise HTTPException(status_code=404, detail="Document not found.")

    _require_class_access(db=db, user=current_user, class_id=record.class_id)
    if record.mime_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Highlighting is only available for PDF files.")
    try:
        path = document_service.document_path(record)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document file is not available.") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file is not available.")

    content = pdf_highlight_service.highlight_pdf(path, payload.page, payload.passages)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{record.file_name}"'},
    )


@router.post("/sources/{document_id}/rects")
def highlight_source_rects(
    document_id: uuid.UUID,
    payload: HighlightRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return lightweight bounding boxes for cited passages without transferring full PDF."""

    record = db.query(Document).filter(Document.id == document_id).first()
    if record is None or record.status != document_service.STATUS_INDEXED:
        raise HTTPException(status_code=404, detail="Document not found.")

    _require_class_access(db=db, user=current_user, class_id=record.class_id)
    if record.mime_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Highlighting is only available for PDF files.")
    try:
        path = document_service.document_path(record)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document file is not available.") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file is not available.")

    rects = pdf_highlight_service.get_highlight_rects(path, payload.page, payload.passages)
    return {"document_id": str(document_id), "page": payload.page, "rects": rects}


@router.post("")
async def chat(
    payload: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Answer one question using sources scoped to the requested class."""

    _require_class_access(db=db, user=current_user, class_id=payload.class_id)

    history = []
    if payload.session_id is not None:
        history = chat_context_service.load_recent_exchanges(
            db=db,
            session_id=payload.session_id,
            user_id=current_user.id,
            class_id=payload.class_id,
        )

    return StreamingResponse(
        chat_service.stream_chat(
            db=db,
            message=payload.message,
            class_id=payload.class_id,
            user_id=current_user.id,
            session_id=payload.session_id,
            history=history,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
