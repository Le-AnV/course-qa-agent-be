"""Attendance management service (AI-136 Phase 2).

Provides core attendance operations:
- get_sheet: fetch full attendance sheet for a class session with status counts.
- save_sheet: partial/bulk upsert attendance entries with strict validation (AC-01, AC-02, AC-05).
- get_summary: class-wide attendance rate summary per student (AC-04, Decision D12).
- get_student_history: student's own attendance history for a class (AC-03).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.attendance import Attendance, ClassSession
from app.models.course import Class, Enrollment
from app.models.user import User
from app.schemas.attendance import (
    AttendanceCounts,
    AttendanceRecordRead,
    AttendanceRecordUpdate,
    AttendanceSheetRead,
    AttendanceSummaryRead,
    AttendanceSummaryRow,
    ClassSessionRead,
    StudentAttendanceItem,
    StudentAttendanceRead,
)
from app.services import schedule_service
from app.services.course_service import DomainError
from app.services.holiday_service import VIETNAM_TZ, local_date

logger = logging.getLogger(__name__)


class SessionNotFoundError(DomainError):
    """Raised when a requested class session is not found in the class."""


class CancelledSessionError(DomainError):
    """Raised when attempting to modify attendance for a cancelled session."""


class FutureSessionError(DomainError):
    """Raised when attempting to mark attendance for a session in the future."""


class StudentNotInRosterError(DomainError):
    """Raised when a student is not an active enrolled member of the class."""


def get_sheet(
    db: Session,
    clazz: Class,
    session_id: uuid.UUID,
) -> AttendanceSheetRead:
    """Return the full attendance sheet for a session with status counts.

    Includes all ACTIVE students in the class roster ordered by student_code
    ascending then full_name ascending. Unmarked students receive status=None.
    """
    session = (
        db.query(ClassSession)
        .filter(ClassSession.id == session_id, ClassSession.class_id == clazz.id)
        .first()
    )
    if session is None:
        raise SessionNotFoundError(f"Session {session_id} not found in class {clazz.id}.")

    # 1. Fetch active class roster
    roster = (
        db.query(User)
        .join(Enrollment, Enrollment.student_id == User.id)
        .filter(Enrollment.class_id == clazz.id, Enrollment.status == "ACTIVE")
        .order_by(User.student_code.asc(), User.full_name.asc())
        .all()
    )

    # 2. Fetch existing attendance records for this session
    attendance_rows = (
        db.query(Attendance)
        .filter(
            Attendance.class_session_id == session.id,
            Attendance.student_id.isnot(None),
        )
        .all()
    )
    attendance_map: dict[uuid.UUID, Attendance] = {
        row.student_id: row for row in attendance_rows if row.student_id is not None
    }

    # 3. Build records and status counts
    records: list[AttendanceRecordRead] = []
    present_cnt = 0
    late_cnt = 0
    excused_cnt = 0
    unexcused_cnt = 0

    for student in roster:
        att = attendance_map.get(student.id)
        status_val = att.status if att else None
        note_val = att.note if att else None
        updated_at_val = att.updated_at if att else None

        if status_val == "present":
            present_cnt += 1
        elif status_val == "late":
            late_cnt += 1
        elif status_val == "excused_absence":
            excused_cnt += 1
        elif status_val == "unexcused_absence":
            unexcused_cnt += 1

        records.append(
            AttendanceRecordRead(
                student_id=student.id,
                student_code=student.student_code,
                full_name=student.full_name,
                email=student.email,
                status=status_val,
                note=note_val,
                updated_at=updated_at_val,
            )
        )

    unmarked_cnt = len(roster) - (present_cnt + late_cnt + excused_cnt + unexcused_cnt)

    summary = AttendanceCounts(
        present=present_cnt,
        late=late_cnt,
        excused_absence=excused_cnt,
        unexcused_absence=unexcused_cnt,
        unmarked=max(0, unmarked_cnt),
    )

    trainer_name = None
    trainer_user = db.query(User).filter(User.id == session.trainer_id).first()
    if trainer_user:
        trainer_name = trainer_user.full_name

    session_read = ClassSessionRead(
        id=session.id,
        class_id=session.class_id,
        schedule_id=session.schedule_id,
        date=session.date,
        start_at=session.start_at,
        end_at=session.end_at,
        trainer_id=session.trainer_id,
        trainer_name=trainer_name,
        status=session.status,
        note=session.note,
        student_count=len(roster),
        marked_count=len(attendance_rows),
    )

    return AttendanceSheetRead(
        session=session_read,
        summary=summary,
        records=records,
    )


def save_sheet(
    db: Session,
    clazz: Class,
    session_id: uuid.UUID,
    records: list[AttendanceRecordUpdate],
    recorded_by: uuid.UUID,
    *,
    today: date | None = None,
) -> AttendanceSheetRead:
    """Upsert attendance records for students in a class session.

    Validations:
    - Session must exist in class and not be cancelled (D9).
    - Session date must be <= today in Vietnam time (D3, D8).
    - Every student in records must be an ACTIVE member of the class.

    Updates existing records (modifying status, note, recorded_by and triggering updated_at)
    or inserts new records.
    """
    session = (
        db.query(ClassSession)
        .filter(ClassSession.id == session_id, ClassSession.class_id == clazz.id)
        .first()
    )
    if session is None:
        raise SessionNotFoundError(f"Session {session_id} not found in class {clazz.id}.")

    if session.status == "cancelled":
        raise CancelledSessionError("Cannot modify attendance for a cancelled session.")

    if today is None:
        today = local_date(datetime.now(VIETNAM_TZ))

    if session.date > today:
        raise FutureSessionError(
            f"Cannot mark attendance for future session on {session.date.isoformat()}."
        )

    # Validate all student IDs belong to active class roster
    active_enrollments = (
        db.query(Enrollment.student_id)
        .filter(Enrollment.class_id == clazz.id, Enrollment.status == "ACTIVE")
        .all()
    )
    active_student_ids = {row[0] for row in active_enrollments}

    for rec in records:
        if rec.student_id not in active_student_ids:
            raise StudentNotInRosterError(
                f"Student {rec.student_id} is not an active member of class {clazz.id}."
            )

    # Helper to apply upsert in a transaction
    def _apply_upsert() -> None:
        student_ids = [r.student_id for r in records]
        existing_rows = (
            db.query(Attendance)
            .filter(
                Attendance.class_session_id == session.id,
                Attendance.student_id.in_(student_ids),
            )
            .all()
        )
        existing_map = {row.student_id: row for row in existing_rows}

        for rec in records:
            existing = existing_map.get(rec.student_id)
            if existing is not None:
                existing.status = rec.status
                existing.note = rec.note
                existing.recorded_by = recorded_by
            else:
                new_att = Attendance(
                    class_session_id=session.id,
                    student_id=rec.student_id,
                    status=rec.status,
                    note=rec.note,
                    recorded_by=recorded_by,
                )
                db.add(new_att)

    try:
        _apply_upsert()
        db.commit()
    except IntegrityError:
        # Retry once in case of concurrent insert race condition
        db.rollback()
        _apply_upsert()
        db.commit()

    return get_sheet(db, clazz, session_id)


def get_summary(
    db: Session,
    clazz: Class,
    date_from: date,
    date_to: date,
    *,
    today: date | None = None,
) -> AttendanceSummaryRead:
    """Return attendance rate summary per student for all sessions in range.

    Rules:
    - Materialises missing sessions in range up to today.
    - Only sessions with status='scheduled' and date <= today are counted.
    - Rate = (present + late) / (present + late + unexcused_absence + unmarked) (D12).
    - excused_absence is excluded from denominator.
    - Unmarked sessions count as absences in denominator.
    """
    if today is None:
        today = local_date(datetime.now(VIETNAM_TZ))

    # Lazy generate sessions in requested range up to today
    gen_to = min(date_to, today)
    if date_from <= gen_to:
        schedule_service.generate_sessions(db, clazz, date_from, gen_to)

    # 1. Fetch countable sessions: scheduled and date <= today
    effective_to = min(date_to, today)
    if date_from > effective_to:
        sessions: list[ClassSession] = []
    else:
        sessions = (
            db.query(ClassSession)
            .filter(
                ClassSession.class_id == clazz.id,
                ClassSession.status == "scheduled",
                ClassSession.date >= date_from,
                ClassSession.date <= effective_to,
            )
            .order_by(ClassSession.start_at.asc())
            .all()
        )

    total_sessions = len(sessions)
    session_ids = [s.id for s in sessions]

    # 2. Fetch active roster
    students = (
        db.query(User)
        .join(Enrollment, Enrollment.student_id == User.id)
        .filter(Enrollment.class_id == clazz.id, Enrollment.status == "ACTIVE")
        .order_by(User.student_code.asc(), User.full_name.asc())
        .all()
    )

    # 3. Fetch attendance rows for these sessions
    attendances: list[Attendance] = []
    if session_ids:
        attendances = (
            db.query(Attendance)
            .filter(
                Attendance.class_session_id.in_(session_ids),
                Attendance.student_id.isnot(None),
            )
            .all()
        )

    student_att_map: dict[uuid.UUID, list[Attendance]] = {s.id: [] for s in students}
    for a in attendances:
        if a.student_id in student_att_map:
            student_att_map[a.student_id].append(a)

    # 4. Aggregate counts per student
    rows: list[AttendanceSummaryRow] = []
    for s in students:
        att_list = student_att_map.get(s.id, [])
        p_cnt = sum(1 for a in att_list if a.status == "present")
        l_cnt = sum(1 for a in att_list if a.status == "late")
        e_cnt = sum(1 for a in att_list if a.status == "excused_absence")
        u_cnt = sum(1 for a in att_list if a.status == "unexcused_absence")
        marked_cnt = len(att_list)
        unmarked_cnt = max(0, total_sessions - marked_cnt)

        denominator = p_cnt + l_cnt + u_cnt + unmarked_cnt
        rate = round((p_cnt + l_cnt) / denominator, 4) if denominator > 0 else None

        rows.append(
            AttendanceSummaryRow(
                student_id=s.id,
                student_code=s.student_code,
                full_name=s.full_name,
                present=p_cnt,
                late=l_cnt,
                excused_absence=e_cnt,
                unexcused_absence=u_cnt,
                unmarked=unmarked_cnt,
                attendance_rate=rate,
            )
        )

    return AttendanceSummaryRead(
        class_id=clazz.id,
        date_from=date_from,
        date_to=date_to,
        total_sessions=total_sessions,
        students=rows,
    )


def get_student_history(
    db: Session,
    clazz: Class,
    student_id: uuid.UUID,
    date_from: date,
    date_to: date,
    *,
    today: date | None = None,
) -> StudentAttendanceRead:
    """Return a student's personal attendance history for a class (AC-03).

    Rules:
    - Caller must be an active enrolled student in the class.
    - All sessions in range with date <= today are included (scheduled, cancelled, rescheduled).
    - Unmarked sessions appear with status=None.
    - Rate is computed over scheduled sessions using Decision D12.
    """
    is_active = (
        db.query(Enrollment)
        .filter(
            Enrollment.class_id == clazz.id,
            Enrollment.student_id == student_id,
            Enrollment.status == "ACTIVE",
        )
        .first()
    )
    if is_active is None:
        raise StudentNotInRosterError(
            f"Student {student_id} is not an active enrolled member of class {clazz.id}."
        )

    if today is None:
        today = local_date(datetime.now(VIETNAM_TZ))

    # Lazy generate sessions in requested range up to today
    gen_to = min(date_to, today)
    if date_from <= gen_to:
        schedule_service.generate_sessions(db, clazz, date_from, gen_to)

    # Fetch all sessions in range with date <= today
    effective_to = min(date_to, today)
    if date_from > effective_to:
        sessions: list[ClassSession] = []
    else:
        sessions = (
            db.query(ClassSession)
            .filter(
                ClassSession.class_id == clazz.id,
                ClassSession.date >= date_from,
                ClassSession.date <= effective_to,
            )
            .order_by(ClassSession.start_at.asc())
            .all()
        )

    session_ids = [s.id for s in sessions]

    # Fetch attendance records for this student
    att_map: dict[uuid.UUID, Attendance] = {}
    if session_ids:
        rows = (
            db.query(Attendance)
            .filter(
                Attendance.class_session_id.in_(session_ids),
                Attendance.student_id == student_id,
            )
            .all()
        )
        att_map = {row.class_session_id: row for row in rows}

    items: list[StudentAttendanceItem] = []
    scheduled_countable = 0
    p_cnt = 0
    l_cnt = 0
    e_cnt = 0
    u_cnt = 0

    for sess in sessions:
        att = att_map.get(sess.id)
        status_val = att.status if att else None
        note_val = att.note if att else None
        updated_at_val = att.updated_at if att else None

        items.append(
            StudentAttendanceItem(
                session_id=sess.id,
                date=sess.date,
                start_at=sess.start_at,
                end_at=sess.end_at,
                session_status=sess.status,
                status=status_val,
                note=note_val,
                updated_at=updated_at_val,
            )
        )

        # Only scheduled sessions count toward attendance rate
        if sess.status == "scheduled":
            scheduled_countable += 1
            if status_val == "present":
                p_cnt += 1
            elif status_val == "late":
                l_cnt += 1
            elif status_val == "excused_absence":
                e_cnt += 1
            elif status_val == "unexcused_absence":
                u_cnt += 1

    unmarked_cnt = max(0, scheduled_countable - (p_cnt + l_cnt + e_cnt + u_cnt))
    denominator = p_cnt + l_cnt + u_cnt + unmarked_cnt
    rate = round((p_cnt + l_cnt) / denominator, 4) if denominator > 0 else None

    return StudentAttendanceRead(
        class_id=clazz.id,
        date_from=date_from,
        date_to=date_to,
        total_sessions=len(items),
        attended=p_cnt + l_cnt,
        attendance_rate=rate,
        items=items,
    )

