"""Teacher-facing quiz submissions, statistics and results export (AI-154).

Mounted next to the AI-151/AI-152 quiz router; it reuses ``_require_class_teacher``
so only the class' primary trainer, co-trainers, or an admin can read results.
"""

from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload

from app.core.deps import get_current_user, get_db
from app.models.course import Class
from app.models.quiz import Quiz, QuizQuestion
from app.models.user import User
from app.routers.quizzes.router import _require_class_teacher
from app.schemas.quiz import QuizStatisticsRead, QuizSubmissionRead
from app.services import quiz_result_service

router = APIRouter(prefix="/api/classes/{class_id}/quizzes", tags=["quiz-results"])


def _get_quiz(
    db: Session, class_id: uuid.UUID, quiz_id: uuid.UUID, *, with_questions: bool = False
) -> Quiz:
    query = db.query(Quiz).filter(Quiz.id == quiz_id, Quiz.class_id == class_id)
    if with_questions:
        query = query.options(selectinload(Quiz.questions).selectinload(QuizQuestion.options))
    quiz = query.one_or_none()
    if quiz is None:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return quiz


@router.get("/{quiz_id}/submissions", response_model=list[QuizSubmissionRead])
def list_quiz_submissions(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[QuizSubmissionRead]:
    """All submitted/expired attempts with score, pass and official flag."""
    _require_class_teacher(db, current_user, class_id)
    quiz = _get_quiz(db, class_id, quiz_id)
    return quiz_result_service.build_submissions(db, quiz)


@router.get("/{quiz_id}/statistics", response_model=QuizStatisticsRead)
def get_quiz_statistics(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> QuizStatisticsRead:
    """Class statistics using one official attempt per student."""
    _require_class_teacher(db, current_user, class_id)
    quiz = _get_quiz(db, class_id, quiz_id, with_questions=True)
    return quiz_result_service.build_statistics(db, quiz)


@router.get("/{quiz_id}/results/export")
def export_quiz_results(
    class_id: uuid.UUID,
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Export the class results workbook (Summary, Submissions, PerQuestion)."""
    clazz: Class = _require_class_teacher(db, current_user, class_id)
    quiz = _get_quiz(db, class_id, quiz_id, with_questions=True)
    submissions = quiz_result_service.build_submissions(db, quiz)
    statistics = quiz_result_service.build_statistics(db, quiz)
    workbook = quiz_result_service.build_results_workbook(quiz, clazz, submissions, statistics)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    filename = f"quiz-{quiz.id}-results.xlsx"
    return StreamingResponse(
        buffer,
        media_type=quiz_result_service.XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
