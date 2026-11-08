"""Unit tests for schedule_service (AI-136 Phase 1, Step 1.5).

Covers:
- resolve_range defaults, clamping, and validation (D5).
- generate_sessions: weekly occurrences, idempotency, effective window clipping (E-01).
- holiday suppression (system and class scope, D6).
- preserve cancelled / rescheduled sessions without overwriting (D7).
- schedule CRUD: overlap detection, trainer authorization, cascade/set null on delete.
- ad-hoc session creation: holiday blocking, slot collision detection.
- list_sessions: student_count and marked_count computation.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (register all models)
from app.core.database import Base
from app.models.attendance import Attendance, ClassSchedule, ClassSession, Holiday
from app.models.course import Class, ClassTrainer, Enrollment
from app.models.user import Role, User
from app.schemas.attendance import ClassScheduleCreate, SessionCreate
from app.services import schedule_service
from app.services.course_service import DomainError
from app.services.holiday_service import VIETNAM_TZ, HolidayBlocked


@pytest.fixture()
def db_session():
    """Create a fresh in-memory SQLite database for testing."""
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
    role_name: str = "trainer",
    full_name: str = "Test Trainer",
) -> User:
    role = db.query(Role).filter(Role.name == role_name).first()
    if not role:
        role = Role(name=role_name)
        db.add(role)
        db.flush()
    user = User(
        email=email,
        full_name=full_name,
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
    code: str = "CS101",
    name: str = "Computer Science 101",
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


# ---------------------------------------------------------------------------
# 1. resolve_range tests
# ---------------------------------------------------------------------------


def test_resolve_range_defaults_and_clamps():
    today = date(2026, 11, 5)

    # Defaults: today - 7 days to today + 56 days
    date_from, date_to = schedule_service.resolve_range(None, None, today=today)
    assert date_from == date(2026, 10, 29)
    assert date_to == date(2026, 12, 31)

    # Partial defaults
    date_from, date_to = schedule_service.resolve_range(date(2026, 11, 1), None, today=today)
    assert date_from == date(2026, 11, 1)
    assert date_to == date(2026, 12, 31)

    # Valid custom range
    date_from, date_to = schedule_service.resolve_range(
        date(2026, 11, 1), date(2026, 11, 30), today=today
    )
    assert date_from == date(2026, 11, 1)
    assert date_to == date(2026, 11, 30)

    # Error: from > to
    with pytest.raises(DomainError, match="must not be after"):
        schedule_service.resolve_range(date(2026, 11, 10), date(2026, 11, 1), today=today)

    # Error: delta > 400 days
    with pytest.raises(DomainError, match="exceeds the maximum limit"):
        schedule_service.resolve_range(date(2026, 1, 1), date(2027, 3, 1), today=today)


# ---------------------------------------------------------------------------
# 2. generate_sessions tests
# ---------------------------------------------------------------------------


def test_generate_sessions_creates_weekly_occurrences(db_session):
    trainer = _make_user(db_session, "trainer1@academic.edu.vn")
    clazz = _make_class(db_session, trainer)

    # Schedule: every Monday (weekday=0) 19:00 - 21:00
    schedule = ClassSchedule(
        class_id=clazz.id,
        subject=clazz.name,
        weekday=0,
        start_time=time(19, 0),
        end_time=time(21, 0),
        trainer_id=trainer.id,
        effective_from=date(2026, 11, 2),
        effective_to=None,
    )
    db_session.add(schedule)
    db_session.commit()

    # Query range covering 3 Mondays: 2026-11-02, 2026-11-09, 2026-11-16
    sessions = schedule_service.generate_sessions(
        db_session, clazz, date(2026, 11, 2), date(2026, 11, 16)
    )

    assert len(sessions) == 3
    dates = [s.date for s in sessions]
    assert dates == [date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16)]
    assert all(s.schedule_id == schedule.id for s in sessions)
    assert all(s.status == "scheduled" for s in sessions)
    assert all(s.trainer_id == trainer.id for s in sessions)


def test_generate_sessions_is_idempotent(db_session):
    trainer = _make_user(db_session, "trainer_idem@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS102")

    schedule = ClassSchedule(
        class_id=clazz.id,
        subject=clazz.name,
        weekday=0,
        start_time=time(19, 0),
        end_time=time(21, 0),
        trainer_id=trainer.id,
        effective_from=date(2026, 11, 2),
    )
    db_session.add(schedule)
    db_session.commit()

    # First generation
    sessions_first = schedule_service.generate_sessions(
        db_session, clazz, date(2026, 11, 2), date(2026, 11, 16)
    )
    assert len(sessions_first) == 3
    first_ids = [s.id for s in sessions_first]

    # Second generation: same range
    sessions_second = schedule_service.generate_sessions(
        db_session, clazz, date(2026, 11, 2), date(2026, 11, 16)
    )
    assert len(sessions_second) == 3
    assert [s.id for s in sessions_second] == first_ids

    # Total rows in DB is still 3
    total_count = (
        db_session.query(ClassSession).filter(ClassSession.class_id == clazz.id).count()
    )
    assert total_count == 3


def test_generate_sessions_skips_holidays(db_session):
    trainer = _make_user(db_session, "trainer_hol@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS103")

    schedule = ClassSchedule(
        class_id=clazz.id,
        subject=clazz.name,
        weekday=0,
        start_time=time(19, 0),
        end_time=time(21, 0),
        trainer_id=trainer.id,
        effective_from=date(2026, 11, 2),
    )
    db_session.add(schedule)

    # Class-scoped holiday on Monday 2026-11-09
    holiday = Holiday(
        name="School Anniversary",
        date=date(2026, 11, 9),
        type="solar",
        scope="class",
        class_id=clazz.id,
    )
    db_session.add(holiday)
    db_session.commit()

    sessions = schedule_service.generate_sessions(
        db_session, clazz, date(2026, 11, 2), date(2026, 11, 16)
    )

    # 2026-11-09 must be suppressed
    dates = [s.date for s in sessions]
    assert date(2026, 11, 9) not in dates
    assert dates == [date(2026, 11, 2), date(2026, 11, 16)]


def test_generate_sessions_respects_effective_window(db_session):
    trainer = _make_user(db_session, "trainer_eff@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS104")

    # Schedule effective strictly between 2026-11-09 and 2026-11-16
    schedule = ClassSchedule(
        class_id=clazz.id,
        subject=clazz.name,
        weekday=0,
        start_time=time(19, 0),
        end_time=time(21, 0),
        trainer_id=trainer.id,
        effective_from=date(2026, 11, 9),
        effective_to=date(2026, 11, 16),
    )
    db_session.add(schedule)
    db_session.commit()

    # Query range from 2026-11-02 to 2026-11-23
    sessions = schedule_service.generate_sessions(
        db_session, clazz, date(2026, 11, 2), date(2026, 11, 23)
    )

    dates = [s.date for s in sessions]
    # 2026-11-02 (before effective_from) and 2026-11-23 (after effective_to) are excluded
    assert dates == [date(2026, 11, 9), date(2026, 11, 16)]


def test_generate_sessions_does_not_touch_cancelled_or_rescheduled(db_session):
    trainer = _make_user(db_session, "trainer_cancel@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS105")

    schedule = ClassSchedule(
        class_id=clazz.id,
        subject=clazz.name,
        weekday=0,
        start_time=time(19, 0),
        end_time=time(21, 0),
        trainer_id=trainer.id,
        effective_from=date(2026, 11, 2),
    )
    db_session.add(schedule)
    db_session.flush()

    # Pre-seed a cancelled session on Monday 2026-11-09
    start_at = datetime.combine(date(2026, 11, 9), time(19, 0), tzinfo=VIETNAM_TZ)
    end_at = datetime.combine(date(2026, 11, 9), time(21, 0), tzinfo=VIETNAM_TZ)
    cancelled_session = ClassSession(
        class_id=clazz.id,
        schedule_id=schedule.id,
        date=date(2026, 11, 9),
        start_at=start_at,
        end_at=end_at,
        trainer_id=trainer.id,
        status="cancelled",
        note="Teacher sick",
    )
    db_session.add(cancelled_session)
    db_session.commit()

    # Run generator across the range
    sessions = schedule_service.generate_sessions(
        db_session, clazz, date(2026, 11, 2), date(2026, 11, 16)
    )

    assert len(sessions) == 3
    session_map = {s.date: s for s in sessions}

    # 11-09 must remain cancelled with original note
    assert session_map[date(2026, 11, 9)].status == "cancelled"
    assert session_map[date(2026, 11, 9)].note == "Teacher sick"
    assert session_map[date(2026, 11, 2)].status == "scheduled"
    assert session_map[date(2026, 11, 16)].status == "scheduled"


# ---------------------------------------------------------------------------
# 3. Schedule CRUD & validation tests
# ---------------------------------------------------------------------------


def test_create_schedule_validations_and_overlap(db_session):
    primary_trainer = _make_user(db_session, "primary@academic.edu.vn", full_name="Primary")
    co_trainer = _make_user(db_session, "cotrainer@academic.edu.vn", full_name="Co Trainer")
    outsider = _make_user(db_session, "outsider@academic.edu.vn", full_name="Outsider")

    clazz = _make_class(db_session, primary_trainer, code="CS106")

    # Add co-trainer link
    db_session.add(ClassTrainer(class_id=clazz.id, trainer_id=co_trainer.id))
    db_session.commit()

    # Success: primary trainer creates schedule
    payload1 = ClassScheduleCreate(
        weekday=0,
        start_time=time(9, 0),
        end_time=time(11, 0),
        effective_from=date(2026, 9, 1),
        trainer_id=primary_trainer.id,
    )
    res1 = schedule_service.create_schedule(db_session, clazz, payload1, primary_trainer)
    assert res1.id is not None
    assert res1.subject == clazz.name
    assert res1.trainer_name == "Primary"

    # Success: co-trainer creates schedule for afternoon
    payload2 = ClassScheduleCreate(
        weekday=0,
        start_time=time(14, 0),
        end_time=time(16, 0),
        effective_from=date(2026, 9, 1),
        trainer_id=co_trainer.id,
    )
    res2 = schedule_service.create_schedule(db_session, clazz, payload2, co_trainer)
    assert res2.trainer_name == "Co Trainer"

    # Reject: outsider trainer not in class staff
    payload_bad_trainer = ClassScheduleCreate(
        weekday=1,
        start_time=time(9, 0),
        end_time=time(11, 0),
        effective_from=date(2026, 9, 1),
        trainer_id=outsider.id,
    )
    with pytest.raises(DomainError, match="not a primary or co-trainer"):
        schedule_service.create_schedule(db_session, clazz, payload_bad_trainer, primary_trainer)

    # Reject: time overlap on same weekday (weekday=0, 10:00 - 12:00 overlaps 9:00 - 11:00)
    payload_overlap = ClassScheduleCreate(
        weekday=0,
        start_time=time(10, 0),
        end_time=time(12, 0),
        effective_from=date(2026, 9, 1),
    )
    with pytest.raises(DomainError, match="Schedule time overlaps with existing schedule"):
        schedule_service.create_schedule(db_session, clazz, payload_overlap, primary_trainer)


def test_delete_schedule_preserves_historical_sessions(db_session):
    trainer = _make_user(db_session, "trainer_del@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS107")

    schedule = ClassSchedule(
        class_id=clazz.id,
        subject=clazz.name,
        weekday=1,
        start_time=time(8, 0),
        end_time=time(10, 0),
        trainer_id=trainer.id,
        effective_from=date(2026, 11, 1),
    )
    db_session.add(schedule)
    db_session.commit()

    # Generate session
    sessions = schedule_service.generate_sessions(
        db_session, clazz, date(2026, 11, 1), date(2026, 11, 10)
    )
    assert len(sessions) > 0
    session_id = sessions[0].id

    # Delete schedule
    deleted = schedule_service.delete_schedule(db_session, clazz, schedule.id)
    assert deleted is True

    # Schedule is removed
    sch_in_db = db_session.query(ClassSchedule).filter(ClassSchedule.id == schedule.id).first()
    assert sch_in_db is None

    # Class session is retained (schedule_id becomes None via FK SET NULL or retains session)
    sess_in_db = db_session.query(ClassSession).filter(ClassSession.id == session_id).first()
    assert sess_in_db is not None
    assert sess_in_db.class_id == clazz.id


# ---------------------------------------------------------------------------
# 4. Ad-hoc session creation tests
# ---------------------------------------------------------------------------


def test_create_ad_hoc_session_rejects_holiday(db_session):
    trainer = _make_user(db_session, "trainer_adhoc_hol@academic.edu.vn")
    clazz = _make_class(db_session, trainer, code="CS108")

    # Holiday on 2026-11-20
    db_session.add(
        Holiday(
            name="Vietnamese Teacher's Day",
            date=date(2026, 11, 20),
            type="solar",
            scope="system",
        )
    )
    db_session.commit()

    payload = SessionCreate(
        date=date(2026, 11, 20),
        start_time=time(19, 0),
        end_time=time(21, 0),
        note="Review test",
    )

    with pytest.raises(HolidayBlocked, match="holiday"):
        schedule_service.create_ad_hoc_session(db_session, clazz, payload, trainer)


def test_create_ad_hoc_session_success_and_collision(db_session):
    trainer = _make_user(db_session, "trainer_adhoc_ok@academic.edu.vn", full_name="Adhoc Trainer")
    clazz = _make_class(db_session, trainer, code="CS109")

    payload = SessionCreate(
        date=date(2026, 11, 21),
        start_time=time(19, 0),
        end_time=time(21, 0),
        note="Make up session",
    )

    res = schedule_service.create_ad_hoc_session(db_session, clazz, payload, trainer)
    assert res.id is not None
    assert res.schedule_id is None
    assert res.date == date(2026, 11, 21)
    assert res.trainer_name == "Adhoc Trainer"
    assert res.note == "Make up session"

    # Attempting to create duplicate ad-hoc session on same slot raises DomainError
    with pytest.raises(DomainError, match="already exists at this date and start time"):
        schedule_service.create_ad_hoc_session(db_session, clazz, payload, trainer)


# ---------------------------------------------------------------------------
# 5. list_sessions counts tests
# ---------------------------------------------------------------------------


def test_list_sessions_computes_attendance_counts(db_session):
    trainer = _make_user(db_session, "trainer_counts@academic.edu.vn")
    student1 = _make_user(db_session, "student1@academic.edu.vn", role_name="student")
    student2 = _make_user(db_session, "student2@academic.edu.vn", role_name="student")
    clazz = _make_class(db_session, trainer, code="CS110")

    # Enroll 2 active students
    db_session.add(Enrollment(class_id=clazz.id, student_id=student1.id, status="ACTIVE"))
    db_session.add(Enrollment(class_id=clazz.id, student_id=student2.id, status="ACTIVE"))

    # Weekly schedule on Monday
    schedule = ClassSchedule(
        class_id=clazz.id,
        subject=clazz.name,
        weekday=0,
        start_time=time(19, 0),
        end_time=time(21, 0),
        trainer_id=trainer.id,
        effective_from=date(2026, 11, 2),
    )
    db_session.add(schedule)
    db_session.commit()

    # Generate sessions for 11-02
    sessions = schedule_service.generate_sessions(
        db_session, clazz, date(2026, 11, 2), date(2026, 11, 2)
    )
    assert len(sessions) == 1
    session_obj = sessions[0]

    # Mark attendance for student1 only
    db_session.add(
        Attendance(
            class_session_id=session_obj.id,
            student_id=student1.id,
            status="present",
            recorded_by=trainer.id,
        )
    )
    db_session.commit()

    # Call list_sessions
    res = schedule_service.list_sessions(
        db_session, clazz, date(2026, 11, 2), date(2026, 11, 2)
    )
    assert len(res) == 1
    session_read = res[0]
    assert session_read.student_count == 2
    assert session_read.marked_count == 1
