"""Student attendance history router (AI-136 Phase 2).

Provides endpoint 9:
9. GET /api/student/classes/{class_id}/attendance?from=&to=
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.routers.attendance.permissions import require_enrolled_student
from app.schemas.attendance import StudentAttendanceRead
from app.services import attendance_service, schedule_service
from app.services.attendance_service import StudentNotInRosterError
from app.services.course_service import DomainError

student_router = APIRouter(prefix="/api/student/classes", tags=["attendance"])


@student_router.get(
    "/{class_id}/attendance",
    response_model=StudentAttendanceRead,
    summary="Get caller's personal attendance history for a class",
)
def get_my_attendance_history(
    class_id: uuid.UUID,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StudentAttendanceRead:
    """Return caller's personal attendance history with attendance rate and attended count."""
    clazz = require_enrolled_student(db, current_user, class_id)
    try:
        resolved_from, resolved_to = schedule_service.resolve_range(date_from, date_to)
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        return attendance_service.get_student_history(
            db, clazz, current_user.id, resolved_from, resolved_to
        )
    except StudentNotInRosterError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

