"""Teacher quiz Excel import and validation (AI-152)."""

from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.course import Class
from app.models.quiz import Quiz, QuizOption, QuizQuestion
from app.models.user import User
from app.schemas.quiz import QuizTeacherRead
from app.services import course_service, quiz_excel, user_service

router = APIRouter(prefix="/api/classes/{class_id}/quizzes/import", tags=["quizzes"])


def _require_class_teacher(db: Session, user: User, class_id: uuid.UUID) -> Class:
    clazz = course_service.get_class(db, class_id)
    if clazz is None:
        raise HTTPException(status_code=404, detail="Class not found.")
    role = (user_service.role_name_of(user) or "").upper()
    if role == "ADMIN":
        return clazz
    if role == "TRAINER" and clazz.id in course_service.class_ids_for_trainer(db, user.id):
        return clazz
    raise HTTPException(status_code=403, detail="Only this class' teachers can import quizzes.")


async def _read_xlsx(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=415, detail="Upload an .xlsx file.")
    content = await file.read(quiz_excel.MAX_QUIZ_EXCEL_SIZE_BYTES + 1)
    if len(content) > quiz_excel.MAX_QUIZ_EXCEL_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Excel file exceeds the 10 MB upload limit.")
    if not content.startswith(b"PK"):
        raise HTTPException(status_code=415, detail="Upload a valid .xlsx workbook.")
    return content


def _parse_result(result: quiz_excel.ExcelParseResult) -> dict:
    return {
        "valid": result.is_valid,
        "errors": [error.__dict__ for error in result.errors],
        "warnings": [warning.__dict__ for warning in result.warnings],
        "questions": [question.model_dump(mode="json") for question in result.questions],
    }


@router.post("/preview")
async def preview_quiz_import(
    class_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    _require_class_teacher(db, user, class_id)
    content = await _read_xlsx(file)
    result = quiz_excel.validate_and_parse_quiz_excel(content)
    return _parse_result(result)


@router.post("", response_model=QuizTeacherRead, status_code=status.HTTP_201_CREATED)
async def import_quiz(
    class_id: uuid.UUID,
    title: str = Form(..., min_length=1, max_length=255),
    description: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    _require_class_teacher(db, user, class_id)
    content = await _read_xlsx(file)
    result = quiz_excel.validate_and_parse_quiz_excel(content)
    if not result.is_valid:
        raise HTTPException(status_code=422, detail=_parse_result(result))

    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=422, detail="Quiz title must not be empty.")

    quiz = Quiz(
        class_id=class_id,
        creator_id=user.id,
        title=clean_title,
        description=description.strip() if description and description.strip() else None,
        status="DRAFT",
    )
    for question_data in result.questions:
        grading_config = dict(question_data.grading_config or {})
        if question_data.question_type == "FILL_IN_BLANK":
            grading_config[quiz_excel.FILL_IN_BLANK_ANSWERS_KEY] = question_data.correct_answers
        question = QuizQuestion(
            question_type=question_data.question_type,
            question=question_data.question,
            points=question_data.points,
            difficulty=question_data.difficulty,
            order_index=question_data.order_index,
            explanation=question_data.explanation,
            rubric=question_data.rubric,
            source_reference=question_data.source_reference,
            grading_config=grading_config or None,
            options=[
                QuizOption(
                    option_label=option.option_label,
                    option_text=option.option_text,
                    is_correct=option.is_correct,
                    order_index=option.order_index,
                )
                for option in question_data.options
            ],
        )
        quiz.questions.append(question)

    db.add(quiz)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    db.refresh(quiz)
    return QuizTeacherRead.model_validate(quiz)


@router.get("/template")
def download_quiz_template(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    _require_class_teacher(db, user, class_id)
    buffer = io.BytesIO()
    quiz_excel.generate_quiz_template_workbook().save(buffer)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="quiz_template.xlsx"'},
    )
