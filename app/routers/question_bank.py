"""Router for Question Bank repository management, AI generation, and Excel import/export (AI-160)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.course import Class
from app.models.user import User
from app.schemas.question_bank import (
    BankQuestionCreate,
    BankQuestionListResponse,
    BankQuestionRead,
    BankQuestionUpdate,
    BulkStatusUpdateRequest,
    DocumentQuestionGenRequest,
    MisconceptionGenRequest,
)
from app.services import course_service, question_bank_service, user_service

router = APIRouter(prefix="/api/classes/{class_id}/question-bank", tags=["question-bank"])


def _require_class_teacher(db: Session, user: User, class_id: uuid.UUID) -> Class:
    """Allow admins, the primary trainer, or co-trainers of this class."""
    section = course_service.get_class(db, class_id)
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    role = (user_service.role_name_of(user) or "").upper()
    if role == "ADMIN":
        return section
    if role == "TRAINER" and section.id in course_service.class_ids_for_trainer(db, user.id):
        return section
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only an administrator or a trainer of this class can manage the question bank.",
    )


@router.get("", response_model=BankQuestionListResponse)
def list_questions(
    class_id: uuid.UUID,
    skill_id: Annotated[uuid.UUID | None, Query(description="Filter by linked skill ID")] = None,
    question_type: Annotated[str | None, Query(description="Filter by type (SINGLE_CHOICE, MULTIPLE_CHOICE, FILL_IN_BLANK)")] = None,
    difficulty: Annotated[str | None, Query(description="Filter by difficulty")] = None,
    status_filter: Annotated[str | None, Query(alias="status", description="Filter by status (DRAFT, APPROVED, ARCHIVED, ALL)")] = None,
    source_type: Annotated[str | None, Query(description="Filter by source type")] = None,
    search: Annotated[str | None, Query(description="Text search query")] = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankQuestionListResponse:
    """List and search questions in the class repository with aggregated metrics."""
    _require_class_teacher(db, user, class_id)
    return question_bank_service.list_bank_questions(
        db=db,
        class_id=class_id,
        skill_id=skill_id,
        question_type=question_type,
        difficulty=difficulty,
        status=status_filter,
        source_type=source_type,
        search=search,
        skip=skip,
        limit=limit,
    )


@router.post("", response_model=BankQuestionRead, status_code=status.HTTP_201_CREATED)
def create_question(
    class_id: uuid.UUID,
    payload: BankQuestionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankQuestionRead:
    """Manually add a new question to the class repository."""
    _require_class_teacher(db, user, class_id)
    created = question_bank_service.create_bank_question(
        db=db,
        class_id=class_id,
        payload=payload,
        user_id=user.id,
    )
    return question_bank_service._to_read_dto(created)


@router.get("/export-excel")
def export_bank_excel(
    class_id: uuid.UUID,
    skill_id: Annotated[uuid.UUID | None, Query()] = None,
    question_type: Annotated[str | None, Query()] = None,
    difficulty: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Export filtered repository questions to an Excel workbook adhering to the 14-column contract."""
    section = _require_class_teacher(db, user, class_id)
    excel_stream = question_bank_service.export_bank_to_excel(
        db=db,
        class_id=class_id,
        skill_id=skill_id,
        question_type=question_type,
        difficulty=difficulty,
        status=status_filter,
    )
    filename = f"question_bank_{section.code}_{uuid.uuid4().hex[:6]}.xlsx"
    return StreamingResponse(
        excel_stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import-excel")
def import_bank_excel(
    class_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Import questions from a 14-column Excel template into the Question Bank."""
    _require_class_teacher(db, user, class_id)
    file_bytes = file.file.read()
    try:
        count, warnings = question_bank_service.import_bank_from_excel(
            db=db,
            class_id=class_id,
            file_bytes=file_bytes,
            user_id=user.id,
        )
        return {"imported_count": count, "warnings": warnings}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/generate-from-documents", response_model=list[BankQuestionRead])
def generate_from_documents(
    class_id: uuid.UUID,
    payload: DocumentQuestionGenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[BankQuestionRead]:
    """AI synthesize diagnostic questions from uploaded learning materials and syllabus units."""
    _require_class_teacher(db, user, class_id)
    try:
        created = question_bank_service.generate_questions_from_documents(
            db=db,
            class_id=class_id,
            req=payload,
            user_id=user.id,
        )
        return [question_bank_service._to_read_dto(q) for q in created]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to generate questions: {exc}") from exc


@router.post("/mine-misconceptions", response_model=list[BankQuestionRead])
def mine_misconceptions(
    class_id: uuid.UUID,
    payload: MisconceptionGenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[BankQuestionRead]:
    """Analyze student chat confusion and tickets to generate diagnostic probe questions."""
    _require_class_teacher(db, user, class_id)
    try:
        created = question_bank_service.mine_misconceptions(
            db=db,
            class_id=class_id,
            req=payload,
            user_id=user.id,
        )
        return [question_bank_service._to_read_dto(q) for q in created]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to mine misconceptions: {exc}") from exc


@router.post("/bulk-status")
def bulk_status_update(
    class_id: uuid.UUID,
    payload: BulkStatusUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Bulk update question status (DRAFT, APPROVED, ARCHIVED)."""
    _require_class_teacher(db, user, class_id)
    updated = question_bank_service.bulk_update_status(
        db=db,
        class_id=class_id,
        question_ids=payload.question_ids,
        status=payload.status,
    )
    return {"updated_count": updated}


@router.get("/{question_id}", response_model=BankQuestionRead)
def get_question(
    class_id: uuid.UUID,
    question_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankQuestionRead:
    """Retrieve details for a single bank question."""
    _require_class_teacher(db, user, class_id)
    q = question_bank_service.get_bank_question(db, class_id, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Question not found in repository.")
    return question_bank_service._to_read_dto(q)


@router.put("/{question_id}", response_model=BankQuestionRead)
def update_question(
    class_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: BankQuestionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankQuestionRead:
    """Update question content, rubrics, options, or status."""
    _require_class_teacher(db, user, class_id)
    try:
        updated = question_bank_service.update_bank_question(
            db=db,
            class_id=class_id,
            question_id=question_id,
            payload=payload,
        )
        return question_bank_service._to_read_dto(updated)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(
    class_id: uuid.UUID,
    question_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Delete a question from the repository."""
    _require_class_teacher(db, user, class_id)
    deleted = question_bank_service.delete_bank_question(db, class_id, question_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Question not found in repository.")

