"""Unit tests for attendance_service (AI-136 Phase 2, Step 2.4).

Covers:
- get_sheet: active roster with unmarked null status, status counts, 404 on missing session.
- save_sheet: upsert 1 row per student, idempotency, updated_at change, partial updates.
- save_sheet validations: future session rejected (D8), cancelled session rejected (D9), non-roster student rejected (422).
- get_summary: attendance rate formula (D12: excused excluded from denominator, unmarked counts as absence), rate null on zero denominator, ignores future/cancelled sessions.
- get_student_history: returns all past sessions (including cancelled), attended count, no leakage of other students' data (AC-03).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.core.database import Base
from app.models.attendance import Attendance, ClassSession
from app.models.course import Class, Enrollment
from app.models.user import Role, User
from app.schemas.attendance import AttendanceRecordUpdate
from app.services import attendance_service
from app.services.attendance_service import (
    CancelledSessionError,
    FutureSessionError,
    SessionNotFoundError,
    StudentNotInRosterError,
)
from app.services.holiday_service import VIETNAM_TZ


@pytest.fixture()
def db_session():
    """Create fresh in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _make_user(
    db: Session,
    email: str,
    role_name: str = "student",
    full_name: str = "Test Student",
    student_code: str | None = None,
) -> User:
    role = db.query(Role).filter(Role.name == role_name).first()
    if not role:
        role = Role(name=role_name)
        db.add(role)
        db.flush()
    user = User(
        email=email,
        full_name=full_name,
        student_code=student_code,
        role_id=role.id,
        status="ACTIVE",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_class(
    db: Session,
    trainer: User,
    code: str = "CS201",
    name: str = "Data Structures",
) -> Class:
    clazz = Class(
        code=code,
        name=name,
        trainer_id=trainer.id,
        status="ACTIVE",
        semester="HK1",
        academic_year="2026-2027",
        credits=3,
    )
    db.add(clazz)
    db.commit()
    db.refresh(clazz)
    return clazz


def _make_session(
    db: Session,
    clazz: Class,
    session_date: date,
    trainer: User,
    status: str = "scheduled",
    note: str | None = None,
) -> ClassSession:
    start_at = datetime.combine(session_date, time(9, 0), tzinfo=VIETNAM_TZ)
    end_at = datetime.combine(session_date, time(11, 0), tzinfo=VIETNAM_TZ)
    session = ClassSession(
        class_id=clazz.id,
        schedule_id=None,
        date=session_date,
        start_at=start_at,
        end_at=end_at,
        trainer_id=trainer.id,
        status=status,
        note=note,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


# ---------------------------------------------------------------------------
# 1. get_sheet tests
# ---------------------------------------------------------------------------


def test_get_sheet_lists_all_active_students_with_null_status(db_session):
    trainer = _make_user(db_session, "trainer_sheet@academic.edu.vn", role_name="trainer")
    student1 = _make_user(
        db_session, "s1_sheet@academic.edu.vn", full_name="Alice", student_code="STU001"
    )
    student2 = _make_user(
        db_session, "s2_sheet@academic.edu.vn", full_name="Bob", student_code="STU002"
    )
    clazz = _make_class(db_session, trainer, code="CS202")

    # Enroll 2 active students
    db_session.add(Enrollment(class_id=clazz.id, student_id=student1.id, status="ACTIVE"))
    db_session.add(Enrollment(class_id=clazz.id, student_id=student2.id, status="ACTIVE"))
    db_session.commit()

    session = _make_session(db_session, clazz, date(2026, 11, 2), trainer)

    # Call get_sheet before any attendance is recorded
    sheet = attendance_service.get_sheet(db_session, clazz, session.id)

    assert sheet.session.id == session.id
    assert sheet.session.student_count == 2
    assert sheet.session.marked_count == 0

    assert len(sheet.records) == 2
    assert sheet.records[0].student_code == "STU001"
    assert sheet.records[0].status is None
    assert sheet.records[0].updated_at is None
    assert sheet.records[1].student_code == "STU002"
    assert sheet.records[1].status is None

    # Summary counts
    assert sheet.summary.unmarked == 2
    assert sheet.summary.present == 0
    assert sheet.summary.late == 0
    assert sheet.summary.excused_absence == 0
    assert sheet.summary.unexcused_absence == 0


def test_get_sheet_raises_not_found(db_session):
    trainer = _make_user(db_session, "trainer_404@academic.edu.vn", role_name="trainer")
    clazz = _make_class(db_session, trainer, code="CS203")

    with pytest.raises(SessionNotFoundError):
        attendance_service.get_sheet(db_session, clazz, uuid.uuid4())


# ---------------------------------------------------------------------------
# 2. save_sheet tests & validations
# ---------------------------------------------------------------------------


def test_save_sheet_creates_and_upserts_records(db_session):
    trainer = _make_user(db_session, "trainer_save@academic.edu.vn", role_name="trainer")
    student1 = _make_user(db_session, "s1_save@academic.edu.vn", student_code="S01")
    student2 = _make_user(db_session, "s2_save@academic.edu.vn", student_code="S02")
    clazz = _make_class(db_session, trainer, code="CS204")

    db_session.add(Enrollment(class_id=clazz.id, student_id=student1.id, status="ACTIVE"))
    db_session.add(Enrollment(class_id=clazz.id, student_id=student2.id, status="ACTIVE"))
    db_session.commit()

    today = date(2026, 11, 5)
    session = _make_session(db_session, clazz, date(2026, 11, 2), trainer)

    # 1. Initial save: student1 present, student2 late
    payload = [
        AttendanceRecordUpdate(student_id=student1.id, status="present", note="on time"),
        AttendanceRecordUpdate(student_id=student2.id, status="late", note="traffic"),
    ]
    sheet1 = attendance_service.save_sheet(
        db_session, clazz, session.id, payload, trainer.id, today=today
    )

    assert sheet1.summary.present == 1
    assert sheet1.summary.late == 1
    assert sheet1.summary.unmarked == 0
    assert sheet1.session.marked_count == 2

    rec1 = next(r for r in sheet1.records if r.student_id == student1.id)
    assert rec1.status == "present"
    assert rec1.note == "on time"
    assert rec1.updated_at is not None
    initial_updated_at = rec1.updated_at

    # 2. Upsert (idempotent & update): change student1 to excused_absence
    payload2 = [
        AttendanceRecordUpdate(
            student_id=student1.id, status="excused_absence", note="doctor note"
        )
    ]
    sheet2 = attendance_service.save_sheet(
        db_session, clazz, session.id, payload2, trainer.id, today=today
    )

    # Row count for student1 in DB remains exactly 1
    total_rows = (
        db_session.query(Attendance)
        .filter(
            Attendance.class_session_id == session.id,
            Attendance.student_id == student1.id,
        )
        .count()
    )
    assert total_rows == 1

    rec1_updated = next(r for r in sheet2.records if r.student_id == student1.id)
    assert rec1_updated.status == "excused_absence"
    assert rec1_updated.note == "doctor note"
    assert rec1_updated.updated_at is not None
    assert rec1_updated.updated_at >= initial_updated_at

    # Student 2 remains late (partial update leaves other rows untouched)
    rec2_untouched = next(r for r in sheet2.records if r.student_id == student2.id)
    assert rec2_untouched.status == "late"
    assert rec2_untouched.note == "traffic"

    # Status counts updated
    assert sheet2.summary.present == 0
    assert sheet2.summary.late == 1
    assert sheet2.summary.excused_absence == 1
    assert sheet2.summary.unmarked == 0


def test_save_sheet_rejects_future_session(db_session):
    trainer = _make_user(db_session, "trainer_fut@academic.edu.vn", role_name="trainer")
    student = _make_user(db_session, "s_fut@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS205")
    db_session.add(Enrollment(class_id=clazz.id, student_id=student.id, status="ACTIVE"))
    db_session.commit()

    today = date(2026, 11, 5)
    future_date = date(2026, 11, 6)
    session = _make_session(db_session, clazz, future_date, trainer)

    payload = [AttendanceRecordUpdate(student_id=student.id, status="present")]

    with pytest.raises(FutureSessionError, match="future session"):
        attendance_service.save_sheet(
            db_session, clazz, session.id, payload, trainer.id, today=today
        )


def test_save_sheet_rejects_cancelled_session(db_session):
    trainer = _make_user(db_session, "trainer_canc@academic.edu.vn", role_name="trainer")
    student = _make_user(db_session, "s_canc@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS206")
    db_session.add(Enrollment(class_id=clazz.id, student_id=student.id, status="ACTIVE"))
    db_session.commit()

    today = date(2026, 11, 5)
    session = _make_session(
        db_session, clazz, date(2026, 11, 2), trainer, status="cancelled"
    )

    payload = [AttendanceRecordUpdate(student_id=student.id, status="present")]

    with pytest.raises(CancelledSessionError, match="cancelled session"):
        attendance_service.save_sheet(
            db_session, clazz, session.id, payload, trainer.id, today=today
        )


def test_save_sheet_rejects_student_not_in_roster(db_session):
    trainer = _make_user(db_session, "trainer_roster@academic.edu.vn", role_name="trainer")
    enrolled_stu = _make_user(db_session, "enrolled@academic.edu.vn")
    outsider_stu = _make_user(db_session, "outsider@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS207")

    db_session.add(Enrollment(class_id=clazz.id, student_id=enrolled_stu.id, status="ACTIVE"))
    db_session.commit()

    today = date(2026, 11, 5)
    session = _make_session(db_session, clazz, date(2026, 11, 2), trainer)

    payload = [AttendanceRecordUpdate(student_id=outsider_stu.id, status="present")]

    with pytest.raises(StudentNotInRosterError, match="not an active member"):
        attendance_service.save_sheet(
            db_session, clazz, session.id, payload, trainer.id, today=today
        )


# ---------------------------------------------------------------------------
# 3. get_summary tests (D12 rate formula)
# ---------------------------------------------------------------------------


def test_summary_counts_and_rate(db_session):
    trainer = _make_user(db_session, "trainer_sum@academic.edu.vn", role_name="trainer")
    student1 = _make_user(db_session, "s1_sum@academic.edu.vn", student_code="A01")
    student2 = _make_user(db_session, "s2_sum@academic.edu.vn", student_code="A02")
    clazz = _make_class(db_session, trainer, code="CS208")

    db_session.add(Enrollment(class_id=clazz.id, student_id=student1.id, status="ACTIVE"))
    db_session.add(Enrollment(class_id=clazz.id, student_id=student2.id, status="ACTIVE"))
    db_session.commit()

    today = date(2026, 11, 5)
    # 3 scheduled sessions in range
    s1 = _make_session(db_session, clazz, date(2026, 11, 2), trainer)
    s2 = _make_session(db_session, clazz, date(2026, 11, 3), trainer)
    s3 = _make_session(db_session, clazz, date(2026, 11, 4), trainer)

    # Student 1:
    # Day 1: present, Day 2: late, Day 3: unmarked
    # denominator = 1 (present) + 1 (late) + 0 (unexcused) + 1 (unmarked) = 3
    # rate = (1 + 1) / 3 = 0.6667
    db_session.add(
        Attendance(
            class_session_id=s1.id,
            student_id=student1.id,
            status="present",
            recorded_by=trainer.id,
        )
    )
    db_session.add(
        Attendance(
            class_session_id=s2.id,
            student_id=student1.id,
            status="late",
            recorded_by=trainer.id,
        )
    )

    # Student 2:
    # Day 1: present, Day 2: excused_absence, Day 3: unexcused_absence
    # excused_absence is EXCLUDED from denominator!
    # denominator = 1 (present) + 0 (late) + 1 (unexcused) + 0 (unmarked) = 2
    # rate = 1 / 2 = 0.5
    db_session.add(
        Attendance(
            class_session_id=s1.id,
            student_id=student2.id,
            status="present",
            recorded_by=trainer.id,
        )
    )
    db_session.add(
        Attendance(
            class_session_id=s2.id,
            student_id=student2.id,
            status="excused_absence",
            recorded_by=trainer.id,
        )
    )
    db_session.add(
        Attendance(
            class_session_id=s3.id,
            student_id=student2.id,
            status="unexcused_absence",
            recorded_by=trainer.id,
        )
    )
    db_session.commit()

    summary = attendance_service.get_summary(
        db_session, clazz, date(2026, 11, 1), date(2026, 11, 10), today=today
    )

    assert summary.total_sessions == 3
    assert len(summary.students) == 2

    row1 = next(r for r in summary.students if r.student_id == student1.id)
    assert row1.present == 1
    assert row1.late == 1
    assert row1.excused_absence == 0
    assert row1.unexcused_absence == 0
    assert row1.unmarked == 1
    assert row1.attendance_rate == 0.6667

    row2 = next(r for r in summary.students if r.student_id == student2.id)
    assert row2.present == 1
    assert row2.late == 0
    assert row2.excused_absence == 1
    assert row2.unexcused_absence == 1
    assert row2.unmarked == 0
    assert row2.attendance_rate == 0.5


def test_summary_rate_is_none_when_no_countable_sessions(db_session):
    trainer = _make_user(db_session, "trainer_zero@academic.edu.vn", role_name="trainer")
    student = _make_user(db_session, "s_zero@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS209")
    db_session.add(Enrollment(class_id=clazz.id, student_id=student.id, status="ACTIVE"))
    db_session.commit()

    today = date(2026, 11, 5)
    # Only 1 session, and student has excused_absence (excluded from denominator)
    s1 = _make_session(db_session, clazz, date(2026, 11, 2), trainer)
    db_session.add(
        Attendance(
            class_session_id=s1.id,
            student_id=student.id,
            status="excused_absence",
            recorded_by=trainer.id,
        )
    )
    db_session.commit()

    summary = attendance_service.get_summary(
        db_session, clazz, date(2026, 11, 1), date(2026, 11, 5), today=today
    )

    assert summary.total_sessions == 1
    row = summary.students[0]
    assert row.excused_absence == 1
    assert row.attendance_rate is None


def test_summary_ignores_future_and_cancelled_sessions(db_session):
    trainer = _make_user(db_session, "trainer_ign@academic.edu.vn", role_name="trainer")
    student = _make_user(db_session, "s_ign@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS210")
    db_session.add(Enrollment(class_id=clazz.id, student_id=student.id, status="ACTIVE"))
    db_session.commit()

    today = date(2026, 11, 5)

    # 1. Past scheduled session (counted)
    _make_session(db_session, clazz, date(2026, 11, 2), trainer, status="scheduled")
    # 2. Past cancelled session (ignored in summary total)
    _make_session(db_session, clazz, date(2026, 11, 3), trainer, status="cancelled")
    # 3. Future scheduled session (ignored because date > today)
    _make_session(db_session, clazz, date(2026, 11, 10), trainer, status="scheduled")

    summary = attendance_service.get_summary(
        db_session, clazz, date(2026, 11, 1), date(2026, 11, 20), today=today
    )

    # Only session 1 is countable
    assert summary.total_sessions == 1


# ---------------------------------------------------------------------------
# 4. get_student_history tests (AC-03)
# ---------------------------------------------------------------------------


def test_student_history_returns_all_past_sessions(db_session):
    trainer = _make_user(db_session, "trainer_hist@academic.edu.vn", role_name="trainer")
    student1 = _make_user(db_session, "s1_hist@academic.edu.vn")
    student2 = _make_user(db_session, "s2_hist@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS211")

    db_session.add(Enrollment(class_id=clazz.id, student_id=student1.id, status="ACTIVE"))
    db_session.add(Enrollment(class_id=clazz.id, student_id=student2.id, status="ACTIVE"))
    db_session.commit()

    today = date(2026, 11, 5)

    # 2 sessions: 1 scheduled, 1 cancelled
    s1 = _make_session(db_session, clazz, date(2026, 11, 2), trainer, status="scheduled")
    s2 = _make_session(
        db_session,
        clazz,
        date(2026, 11, 4),
        trainer,
        status="cancelled",
        note="Holiday off",
    )

    # Mark student1 present on s1; mark student2 late on s1
    db_session.add(
        Attendance(
            class_session_id=s1.id,
            student_id=student1.id,
            status="present",
            note="Good participation",
            recorded_by=trainer.id,
        )
    )
    db_session.add(
        Attendance(
            class_session_id=s1.id,
            student_id=student2.id,
            status="late",
            note="Private note for student 2",
            recorded_by=trainer.id,
        )
    )
    db_session.commit()

    history = attendance_service.get_student_history(
        db_session, clazz, student1.id, date(2026, 11, 1), date(2026, 11, 10), today=today
    )

    assert history.total_sessions == 2
    assert history.attended == 1
    assert history.attendance_rate == 1.0  # 1 scheduled session, present=1 -> 1.0

    items = history.items
    assert len(items) == 2

    # Session 1: scheduled & marked
    assert items[0].session_id == s1.id
    assert items[0].session_status == "scheduled"
    assert items[0].status == "present"
    assert items[0].note == "Good participation"

    # Session 2: cancelled & unmarked
    assert items[1].session_id == s2.id
    assert items[1].session_status == "cancelled"
    assert items[1].status is None
    assert items[1].note is None

    # Verify student 2's note did not leak into student 1's history
    assert all("Private note for student 2" not in (it.note or "") for it in items)


def test_student_history_rejects_non_enrolled_student(db_session):
    trainer = _make_user(db_session, "trainer_noenroll@academic.edu.vn", role_name="trainer")
    student = _make_user(db_session, "s_noenroll@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS212")

    today = date(2026, 11, 5)

    with pytest.raises(StudentNotInRosterError, match="not an active enrolled member"):
        attendance_service.get_student_history(
            db_session, clazz, student.id, date(2026, 11, 1), date(2026, 11, 10), today=today
        )
