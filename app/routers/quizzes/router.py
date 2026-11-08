"""Teacher quiz Excel import, review, approval and export (AI-151, AI-152)."""

from __future__ import annotations

import io
import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.course import Class
from app.models.quiz import Quiz, QuizOption, QuizQuestion
from app.models.user import User
from app.schemas.quiz import (
    QuizQuestionCreate,
    QuizSummaryRead,
    QuizTeacherRead,
    QuizUpdate,
    quiz_teacher_read,
)
from app.services import course_service, quiz_excel, quiz_generation_service, user_service
from app.services import quiz_review_service as review
from app.services.quiz_review_service import QuizStateError

router = APIRouter(prefix="/api/classes/{class_id}/quizzes", tags=["quizzes"])


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


@router.post("/import/preview")
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


@router.post("/import", response_model=QuizTeacherRead, status_code=status.HTTP_201_CREATED)
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


@router.get("/import/template")
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


@router.get("", response_model=list[QuizSummaryRead])
def list_class_quizzes(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[QuizSummaryRead]:
    """List this class' quizzes for the teacher import page."""

    _require_class_teacher(db, user, class_id)
    rows = (
        db.query(Quiz, func.count(QuizQuestion.id))
        .outerjoin(QuizQuestion, QuizQuestion.quiz_id == Quiz.id)
        .filter(Quiz.class_id == class_id)
        .group_by(Quiz.id)
        .order_by(Quiz.created_at.desc())
        .all()
    )
    return [
        QuizSummaryRead(
            id=quiz.id,
            title=quiz.title,
            description=quiz.description,
            status=quiz.status,
            time_limit_minutes=quiz.time_limit_minutes,
            opens_at=quiz.opens_at,
            closes_at=quiz.closes_at,
            max_attempts=quiz.max_attempts,
            question_count=question_count,
            created_at=quiz.created_at,
            updated_at=quiz.updated_at,
        )
        for quiz, question_count in rows
    ]


@router.get("/{quiz_id}", response_model=QuizTeacherRead)
def get_class_quiz(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    """Full quiz with questions, options and accepted answers for review."""

    _require_class_teacher(db, user, class_id)
    return _teacher_read(db, _get_quiz_or_404(db, class_id, quiz_id))


def _teacher_read(db: Session, quiz: Quiz) -> QuizTeacherRead:
    approver = db.get(User, quiz.approved_by_id) if quiz.approved_by_id else None
    return quiz_teacher_read(
        quiz, approved_by_name=approver.full_name if approver is not None else None
    )


def _get_quiz_or_404(db: Session, class_id: uuid.UUID, quiz_id: uuid.UUID) -> Quiz:
    quiz = review.get_quiz(db, class_id, quiz_id)
    if quiz is None:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return quiz


def _state_error(exc: QuizStateError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def _get_question_or_404(quiz: Quiz, question_id: uuid.UUID) -> QuizQuestion:
    question = review.get_question(quiz, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found.")
    return question


def _sanitize_filename(title: str, quiz_id: uuid.UUID) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._")[:60] or "quiz"
    return f"{stem}_{quiz_id}.xlsx"


@router.patch("/{quiz_id}", response_model=QuizTeacherRead)
def update_quiz_metadata(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    payload: QuizUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    """Change title, description, time limit or passing score of a draft quiz."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    try:
        review.update_metadata(db, quiz, payload)
    except QuizStateError as exc:
        raise _state_error(exc) from exc
    return _teacher_read(db, quiz)


@router.post(
    "/{quiz_id}/questions",
    response_model=QuizTeacherRead,
    status_code=status.HTTP_201_CREATED,
)
def add_quiz_question(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    payload: QuizQuestionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    """Append a validated question; ``order_index`` is assigned by the server."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    try:
        review.add_question(db, quiz, payload)
    except QuizStateError as exc:
        raise _state_error(exc) from exc
    return _teacher_read(db, quiz)


@router.put("/{quiz_id}/questions/{question_id}", response_model=QuizTeacherRead)
def replace_quiz_question(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: QuizQuestionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    """Replace a question and its options in place, keeping id and order."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    question = _get_question_or_404(quiz, question_id)
    try:
        review.replace_question(db, quiz, question, payload)
    except QuizStateError as exc:
        raise _state_error(exc) from exc
    return _teacher_read(db, quiz)


@router.delete("/{quiz_id}/questions/{question_id}", response_model=QuizTeacherRead)
def delete_quiz_question(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    question_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    """Delete a question and close the gap in ``order_index``."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    question = _get_question_or_404(quiz, question_id)
    try:
        review.delete_question(db, quiz, question)
    except QuizStateError as exc:
        raise _state_error(exc) from exc
    return _teacher_read(db, quiz)


class RegenerateRequest(BaseModel):
    instructions: str | None = Field(default=None, max_length=500)


@router.post("/{quiz_id}/questions/{question_id}/regenerate", response_model=QuizTeacherRead)
def regenerate_quiz_question(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: RegenerateRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    """Replace one question with a generated one, keeping id and order."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    question = _get_question_or_404(quiz, question_id)
    try:
        generated = quiz_generation_service.regenerate_question(
            db=db,
            question=question,
            instructions=(payload.instructions if payload else None),
            requested_by=user,
        )
    except quiz_generation_service.QuizGenerationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "The generator could not produce a valid question. "
                "Your current question was not changed."
            ),
        ) from exc
    try:
        review.replace_question(db, quiz, question, generated)
    except QuizStateError as exc:
        raise _state_error(exc) from exc
    return _teacher_read(db, quiz)


@router.post("/{quiz_id}/approve", response_model=QuizTeacherRead)
def approve_quiz(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    """Publish a draft quiz and record who approved it, and when (AC-05)."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    try:
        review.approve(db, quiz, user)
    except QuizStateError as exc:
        raise _state_error(exc) from exc
    return _teacher_read(db, quiz)


@router.post("/{quiz_id}/reopen", response_model=QuizTeacherRead)
def reopen_quiz(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizTeacherRead:
    """Return an approved quiz to draft and clear the approval record."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    try:
        review.reopen(db, quiz)
    except QuizStateError as exc:
        raise _state_error(exc) from exc
    return _teacher_read(db, quiz)


@router.get("/{quiz_id}/export")
def export_quiz(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Download the approved quiz as the AI-148 workbook (AC-03, AC-04)."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    try:
        review.require_exportable(quiz)
    except QuizStateError as exc:
        raise _state_error(exc) from exc

    buffer = io.BytesIO()
    quiz_excel.export_quiz_to_workbook(list(quiz.questions)).save(buffer)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_sanitize_filename(quiz.title, quiz.id)}"'
            )
        },
    )


@router.delete("/{quiz_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quiz(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Delete a draft quiz; approved quizzes must be reopened first."""

    _require_class_teacher(db, user, class_id)
    quiz = _get_quiz_or_404(db, class_id, quiz_id)
    try:
        review.delete_quiz(db, quiz)
    except QuizStateError as exc:
        raise _state_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
