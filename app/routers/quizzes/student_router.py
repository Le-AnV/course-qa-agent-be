"""Student quiz list, timed attempts, autosave and submission (AI-153).

Every route is scoped to the caller's own enrollment; an attempt belonging to
another student is reported as 404, never 403.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.quiz import (
    QuizAttemptSessionRead,
    StudentAnswersSave,
    StudentQuizListRead,
)
from app.services import quiz_attempt_service as service
from app.services.quiz_attempt_service import QuizAttemptError

router = APIRouter(prefix="/api/student", tags=["student-quizzes"])


def _http_error(exc: QuizAttemptError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/quizzes", response_model=StudentQuizListRead)
def list_my_quizzes(
    class_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StudentQuizListRead:
    """Published quizzes for the caller's classes, with window and attempts left."""
    return service.list_student_quizzes(db, user, class_id)


@router.post("/quizzes/{quiz_id}/attempts", response_model=QuizAttemptSessionRead)
def start_or_resume_attempt(
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizAttemptSessionRead:
    """Start a new attempt or resume the active one (never leaks answers)."""
    try:
        return service.start_or_resume(db, user, quiz_id)
    except QuizAttemptError as exc:
        raise _http_error(exc) from exc


@router.get("/attempts/{attempt_id}", response_model=QuizAttemptSessionRead)
def get_attempt(
    attempt_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizAttemptSessionRead:
    """Reload a session after refresh/reconnect; auto-submits an expired one."""
    try:
        return service.get_attempt(db, user, attempt_id)
    except QuizAttemptError as exc:
        raise _http_error(exc) from exc


@router.patch("/attempts/{attempt_id}/answers", response_model=QuizAttemptSessionRead)
def save_answers(
    attempt_id: uuid.UUID,
    payload: StudentAnswersSave,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizAttemptSessionRead:
    """Autosave the provided answers; omitted questions keep their draft."""
    try:
        return service.save_draft(db, user, attempt_id, payload.answers)
    except QuizAttemptError as exc:
        raise _http_error(exc) from exc


@router.post("/attempts/{attempt_id}/submit", response_model=QuizAttemptSessionRead)
def submit_attempt(
    attempt_id: uuid.UUID,
    payload: StudentAnswersSave | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QuizAttemptSessionRead:
    """Submit once; repeated clicks return the same attempt and score (AC-06)."""
    try:
        return service.submit_attempt(
            db, user, attempt_id, payload.answers if payload else None
        )
    except QuizAttemptError as exc:
        raise _http_error(exc) from exc
