"""Student quiz attempt history and result review (AI-154 AC-03).

Prefix: /api/student. Every endpoint is ownership-scoped: a caller can only read
their own attempts. Attempts that are still ``IN_PROGRESS`` never expose scores,
correct answers, or explanations.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.core.deps import get_current_user, get_db
from app.models.quiz import Quiz, QuizAttempt, QuizQuestion
from app.models.user import User
from app.schemas.quiz import (
    QuizAttemptHistoryRead,
    QuizAttemptResultRead,
    QuizStudentListRead,
)
from app.services import course_service, quiz_result_service
from app.services.quiz_attempt_service import GRADABLE_ATTEMPT_STATUSES, AttemptGradingError

router = APIRouter(prefix="/api/student", tags=["student-quizzes"])


def _get_quiz(db: Session, quiz_id: uuid.UUID, *, with_questions: bool = False) -> Quiz:
    query = db.query(Quiz).filter(Quiz.id == quiz_id)
    if with_questions:
        query = query.options(selectinload(Quiz.questions).selectinload(QuizQuestion.options))
    quiz = query.one_or_none()
    if quiz is None:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return quiz


@router.get(
    "/classes/{class_id}/quizzes",
    response_model=list[QuizStudentListRead],
)
def list_my_class_quizzes(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[QuizStudentListRead]:
    """Published quizzes in one class with this student's attempt summary."""
    clazz = course_service.get_class(db, class_id)
    if clazz is None:
        raise HTTPException(status_code=404, detail="Class not found.")
    if not course_service.is_active_member(db, class_id, current_user.id):
        raise HTTPException(
            status_code=403,
            detail="You are not enrolled in this class.",
        )
    return quiz_result_service.build_student_quiz_list(db, clazz, current_user.id)


@router.get("/quizzes/{quiz_id}/attempts", response_model=list[QuizAttemptHistoryRead])
def list_my_quiz_attempts(
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[QuizAttemptHistoryRead]:
    """All own attempts for one quiz, newest first; official attempt is marked."""
    quiz = _get_quiz(db, quiz_id)
    try:
        return quiz_result_service.build_history(db, quiz, current_user.id)
    except AttemptGradingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/quizzes/{quiz_id}/attempts/{attempt_id}/result",
    response_model=QuizAttemptResultRead,
)
def get_my_attempt_result(
    quiz_id: uuid.UUID,
    attempt_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> QuizAttemptResultRead:
    """Detailed result for one own submitted attempt.

    Correct answers are revealed only after submission; explanations additionally
    require ``quiz.show_explanations``. Other students' attempts return 404.
    """
    quiz = _get_quiz(db, quiz_id, with_questions=True)
    attempt = (
        db.query(QuizAttempt)
        .filter(
            QuizAttempt.id == attempt_id,
            QuizAttempt.quiz_id == quiz.id,
            QuizAttempt.student_id == current_user.id,
        )
        .one_or_none()
    )
    if attempt is None:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    if attempt.status not in GRADABLE_ATTEMPT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="The attempt has not been submitted yet.",
        )
    try:
        quiz_result_service.ensure_graded(db, [attempt])
    except AttemptGradingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return quiz_result_service.build_attempt_result(db, quiz, attempt)
