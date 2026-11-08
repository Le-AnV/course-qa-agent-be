"""Class schedule and session management router (AI-136 Phase 1 & Phase 2).

Provides endpoints 1–8:
1. GET /api/classes/{class_id}/schedules
2. POST /api/classes/{class_id}/schedules
3. DELETE /api/classes/{class_id}/schedules/{schedule_id}
4. GET /api/classes/{class_id}/sessions?from=&to=
5. POST /api/classes/{class_id}/sessions (ad-hoc session)
6. GET /api/classes/{class_id}/sessions/{session_id}/attendance
7. PUT /api/classes/{class_id}/sessions/{session_id}/attendance
8. GET /api/classes/{class_id}/attendance/summary?from=&to=
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.routers.attendance.permissions import (
    require_class_member_or_staff,
    require_class_staff,
)
from app.schemas.attendance import (
    AttendanceBulkUpdate,
    AttendanceSheetRead,
    AttendanceSummaryRead,
    ClassScheduleCreate,
    ClassScheduleRead,
    ClassSessionRead,
    SessionCreate,
)
from app.services import attendance_service, schedule_service
from app.services.attendance_service import (
    CancelledSessionError,
    FutureSessionError,
    SessionNotFoundError,
    StudentNotInRosterError,
)
from app.services.course_service import DomainError
from app.services.holiday_service import HolidayBlocked

router = APIRouter(prefix="/api/classes", tags=["attendance"])


@router.get(
    "/{class_id}/schedules",
    response_model=list[ClassScheduleRead],
    summary="List recurring weekly schedule templates",
)
def list_class_schedules(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ClassScheduleRead]:
    """Return all recurring weekly schedule slots configured for the class."""
    clazz = require_class_staff(db, current_user, class_id)
    return schedule_service.list_schedules(db, clazz)


@router.post(
    "/{class_id}/schedules",
    response_model=ClassScheduleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a weekly schedule template",
)
def create_class_schedule(
    class_id: uuid.UUID,
    payload: ClassScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClassScheduleRead:
    """Create a recurring weekly schedule slot for the class."""
    clazz = require_class_staff(db, current_user, class_id)
    try:
        return schedule_service.create_schedule(db, clazz, payload, current_user)
    except DomainError as exc:
        msg = str(exc)
        if "overlap" in msg.lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg) from exc
        if "not a primary or co-trainer" in msg.lower():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=msg,
            ) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc


@router.delete(
    "/{class_id}/schedules/{schedule_id}",
    summary="Delete a recurring schedule template",
)
def delete_class_schedule(
    class_id: uuid.UUID,
    schedule_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str | bool]:
    """Delete a weekly schedule template without deleting existing sessions."""
    clazz = require_class_staff(db, current_user, class_id)
    deleted = schedule_service.delete_schedule(db, clazz, schedule_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found.",
        )
    return {"ok": True, "message": "Schedule deleted successfully."}


@router.get(
    "/{class_id}/sessions",
    response_model=list[ClassSessionRead],
    summary="List dated class sessions with attendance counts (materialised lazily)",
)
def list_class_sessions(
    class_id: uuid.UUID,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ClassSessionRead]:
    """Return dated class sessions, materialising missing ones lazily and idempotently."""
    clazz = require_class_member_or_staff(db, current_user, class_id)
    try:
        resolved_from, resolved_to = schedule_service.resolve_range(date_from, date_to)
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return schedule_service.list_sessions(db, clazz, resolved_from, resolved_to)


@router.post(
    "/{class_id}/sessions",
    response_model=ClassSessionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an ad-hoc class session",
)
def create_ad_hoc_session(
    class_id: uuid.UUID,
    payload: SessionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClassSessionRead:
    """Create an ad-hoc unscheduled class session for a specific date."""
    clazz = require_class_staff(db, current_user, class_id)
    try:
        return schedule_service.create_ad_hoc_session(db, clazz, payload, current_user)
    except HolidayBlocked as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot schedule session on a holiday: {exc}",
        ) from exc
    except DomainError as exc:
        msg = str(exc)
        if "already exists" in msg.lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg) from exc
        if "not a primary or co-trainer" in msg.lower():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=msg,
            ) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc


@router.get(
    "/{class_id}/sessions/{session_id}/attendance",
    response_model=AttendanceSheetRead,
    summary="Get full attendance sheet for a class session",
)
def get_session_attendance_sheet(
    class_id: uuid.UUID,
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AttendanceSheetRead:
    """Return the full attendance sheet with active student roster and marked statuses."""
    clazz = require_class_staff(db, current_user, class_id)
    try:
        return attendance_service.get_sheet(db, clazz, session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put(
    "/{class_id}/sessions/{session_id}/attendance",
    response_model=AttendanceSheetRead,
    summary="Upsert attendance entries for students in a class session",
)
def save_session_attendance_sheet(
    class_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: AttendanceBulkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AttendanceSheetRead:
    """Partial or bulk upsert of student attendance records for a session."""
    clazz = require_class_staff(db, current_user, class_id)
    try:
        return attendance_service.save_sheet(
            db, clazz, session_id, payload.records, current_user.id
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CancelledSessionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except FutureSessionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except StudentNotInRosterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/{class_id}/attendance/summary",
    response_model=AttendanceSummaryRead,
    summary="Get class attendance summary and rate per student",
)
def get_class_attendance_summary(
    class_id: uuid.UUID,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AttendanceSummaryRead:
    """Return attendance rate summary per student for all sessions in range."""
    clazz = require_class_staff(db, current_user, class_id)
    try:
        resolved_from, resolved_to = schedule_service.resolve_range(date_from, date_to)
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return attendance_service.get_summary(db, clazz, resolved_from, resolved_to)
