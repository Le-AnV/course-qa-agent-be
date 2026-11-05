"""Schedule and session generation service (AI-136).

Implements the contract specified in AI-155 §4 and AI-136 design:
- Lazy, idempotent session materialisation from recurring class_schedule rows.
- Skip holidays (both system-wide and class-specific via holiday_service).
- Respect effective_from / effective_to date windows.
- Preserve cancelled and rescheduled sessions without recreating them.
- Support ad-hoc session creation with slot collision detection.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.attendance import Attendance, ClassSchedule, ClassSession
from app.models.course import Class, ClassTrainer, Enrollment
from app.models.user import User
from app.schemas.attendance import (
    ClassScheduleCreate,
    ClassScheduleRead,
    ClassSessionRead,
    SessionCreate,
)
from app.services.course_service import DomainError
from app.services.holiday_service import (
    VIETNAM_TZ,
    HolidayBlocked,
    get_holiday,
    holiday_dates,
    is_holiday,
    local_date,
)

logger = logging.getLogger(__name__)

DEFAULT_PAST_DAYS = 7
DEFAULT_FUTURE_DAYS = 56
MAX_RANGE_DAYS = 400


def resolve_range(
    date_from: date | None = None,
    date_to: date | None = None,
    *,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve, clamp, and validate a date range for session queries.

    If date_from or date_to are omitted, defaults are applied relative to
    today (Vietnam local time):
      - date_from: today - 7 days
      - date_to: today + 56 days

    Raises:
        DomainError: if date_from > date_to or range exceeds MAX_RANGE_DAYS.
    """
    if today is None:
        today = local_date(datetime.now(VIETNAM_TZ))

    resolved_from = date_from if date_from is not None else today - timedelta(days=DEFAULT_PAST_DAYS)
    resolved_to = date_to if date_to is not None else today + timedelta(days=DEFAULT_FUTURE_DAYS)

    if resolved_from > resolved_to:
        raise DomainError("date_from must not be after date_to.")

    delta_days = (resolved_to - resolved_from).days
    if delta_days > MAX_RANGE_DAYS:
        raise DomainError(f"Requested date range exceeds the maximum limit of {MAX_RANGE_DAYS} days.")

    return resolved_from, resolved_to


def _is_trainer_allowed_for_class(db: Session, clazz: Class, trainer_id: uuid.UUID) -> bool:
    """Check if the given trainer is primary or co-trainer of the class."""
    if clazz.trainer_id == trainer_id:
        return True
    co_trainer = (
        db.query(ClassTrainer)
        .filter(ClassTrainer.class_id == clazz.id, ClassTrainer.trainer_id == trainer_id)
        .first()
    )
    return co_trainer is not None


def list_schedules(db: Session, clazz: Class) -> list[ClassScheduleRead]:
    """Return all recurring schedules configured for a class with trainer names."""
    rows = (
        db.query(ClassSchedule, User.full_name)
        .outerjoin(User, ClassSchedule.trainer_id == User.id)
        .filter(ClassSchedule.class_id == clazz.id)
        .order_by(ClassSchedule.weekday.asc(), ClassSchedule.start_time.asc())
        .all()
    )

    result: list[ClassScheduleRead] = []
    for schedule, trainer_name in rows:
        dto = ClassScheduleRead(
            id=schedule.id,
            class_id=schedule.class_id,
            subject=schedule.subject,
            weekday=schedule.weekday,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
            trainer_id=schedule.trainer_id,
            trainer_name=trainer_name,
            effective_from=schedule.effective_from,
            effective_to=schedule.effective_to,
        )
        result.append(dto)
    return result


def create_schedule(
    db: Session,
    clazz: Class,
    payload: ClassScheduleCreate,
    actor: User,
) -> ClassScheduleRead:
    """Create a recurring weekly schedule slot for the class.

    Raises:
        DomainError: if trainer_id is invalid or schedule slot overlaps existing slot.
    """
    assigned_trainer_id = payload.trainer_id if payload.trainer_id is not None else clazz.trainer_id
    if not _is_trainer_allowed_for_class(db, clazz, assigned_trainer_id):
        raise DomainError("Assigned trainer is not a primary or co-trainer of this class.")

    # Check time overlap on the same weekday with overlapping effective windows
    existing_schedules = (
        db.query(ClassSchedule)
        .filter(
            ClassSchedule.class_id == clazz.id,
            ClassSchedule.weekday == payload.weekday,
        )
        .all()
    )

    for ex in existing_schedules:
        # Check window overlap
        window_overlap = True
        if payload.effective_to is not None and ex.effective_from > payload.effective_to:
            window_overlap = False
        if ex.effective_to is not None and ex.effective_to < payload.effective_from:
            window_overlap = False

        # Check time overlap: (start_A < end_B) and (start_B < end_A)
        if window_overlap and payload.start_time < ex.end_time and ex.start_time < payload.end_time:
            raise DomainError(
                f"Schedule time overlaps with existing schedule on weekday {payload.weekday} "
                f"({ex.start_time.strftime('%H:%M')} - {ex.end_time.strftime('%H:%M')})."
            )

    subject = clazz.name
    schedule = ClassSchedule(
        class_id=clazz.id,
        subject=subject,
        weekday=payload.weekday,
        start_time=payload.start_time,
        end_time=payload.end_time,
        trainer_id=assigned_trainer_id,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    trainer_name = None
    trainer_user = db.query(User).filter(User.id == assigned_trainer_id).first()
    if trainer_user:
        trainer_name = trainer_user.full_name

    return ClassScheduleRead(
        id=schedule.id,
        class_id=schedule.class_id,
        subject=schedule.subject,
        weekday=schedule.weekday,
        start_time=schedule.start_time,
        end_time=schedule.end_time,
        trainer_id=schedule.trainer_id,
        trainer_name=trainer_name,
        effective_from=schedule.effective_from,
        effective_to=schedule.effective_to,
    )


def delete_schedule(db: Session, clazz: Class, schedule_id: uuid.UUID) -> bool:
    """Delete a recurring schedule slot.

    Does not delete existing materialised class sessions (their schedule_id
    becomes NULL via DB FK ON DELETE SET NULL), preserving attendance history.
    """
    schedule = (
        db.query(ClassSchedule)
        .filter(ClassSchedule.id == schedule_id, ClassSchedule.class_id == clazz.id)
        .first()
    )
    if schedule is None:
        return False

    db.delete(schedule)
    db.commit()
    return True


def generate_sessions(
    db: Session,
    clazz: Class,
    date_from: date,
    date_to: date,
) -> list[ClassSession]:
    """Materialise dated class sessions lazily and idempotently.

    Rules (AI-155 §4):
    1. Active class_schedules whose effective window intersects [date_from, date_to].
    2. Skip holidays (both system scope and class scope).
    3. Idempotent: existing (date, schedule_id) sessions are skipped.
    4. Slot unique (class_id, date, start_at) is respected.
    5. Cancelled/rescheduled sessions are preserved and never overwritten.
    """
    # 1. Fetch schedules effective in this range
    schedules = (
        db.query(ClassSchedule)
        .filter(
            ClassSchedule.class_id == clazz.id,
            ClassSchedule.effective_from <= date_to,
            or_(
                ClassSchedule.effective_to.is_(None),
                ClassSchedule.effective_to >= date_from,
            ),
        )
        .all()
    )

    if not schedules:
        return (
            db.query(ClassSession)
            .filter(
                ClassSession.class_id == clazz.id,
                ClassSession.date >= date_from,
                ClassSession.date <= date_to,
            )
            .order_by(ClassSession.start_at.asc(), ClassSession.id.asc())
            .all()
        )

    # 2. Existing sessions in this range
    existing_sessions = (
        db.query(ClassSession)
        .filter(
            ClassSession.class_id == clazz.id,
            ClassSession.date >= date_from,
            ClassSession.date <= date_to,
        )
        .all()
    )

    # Track existing (date, schedule_id) pairs and (date, start_at) slots
    existing_pairs: set[tuple[date, uuid.UUID]] = {
        (s.date, s.schedule_id) for s in existing_sessions if s.schedule_id is not None
    }
    existing_slots: set[tuple[date, datetime]] = {
        (s.date, s.start_at) for s in existing_sessions
    }

    # 3. Holiday dates in range
    holidays = holiday_dates(db, date_from, date_to, clazz.id)

    # 4. Generate missing occurrences
    new_sessions: list[ClassSession] = []
    current_date = date_from
    while current_date <= date_to:
        if current_date not in holidays:
            current_weekday = current_date.weekday()
            for sch in schedules:
                if sch.weekday != current_weekday:
                    continue
                if current_date < sch.effective_from:
                    continue
                if sch.effective_to is not None and current_date > sch.effective_to:
                    continue
                if (current_date, sch.id) in existing_pairs:
                    continue

                start_at = datetime.combine(current_date, sch.start_time, tzinfo=VIETNAM_TZ)
                end_at = datetime.combine(current_date, sch.end_time, tzinfo=VIETNAM_TZ)

                if (current_date, start_at) in existing_slots:
                    continue

                session = ClassSession(
                    class_id=clazz.id,
                    schedule_id=sch.id,
                    date=current_date,
                    start_at=start_at,
                    end_at=end_at,
                    trainer_id=sch.trainer_id,
                    status="scheduled",
                )
                new_sessions.append(session)
                existing_pairs.add((current_date, sch.id))
                existing_slots.add((current_date, start_at))

        current_date += timedelta(days=1)

    if new_sessions:
        db.add_all(new_sessions)
        try:
            db.commit()
        except IntegrityError:
            # Another concurrent request already inserted the same sessions
            db.rollback()

    return (
        db.query(ClassSession)
        .filter(
            ClassSession.class_id == clazz.id,
            ClassSession.date >= date_from,
            ClassSession.date <= date_to,
        )
        .order_by(ClassSession.start_at.asc(), ClassSession.id.asc())
        .all()
    )


def list_sessions(
    db: Session,
    clazz: Class,
    date_from: date,
    date_to: date,
) -> list[ClassSessionRead]:
    """Materialise sessions if needed and return rich DTOs with attendance counts."""
    sessions = generate_sessions(db, clazz, date_from, date_to)
    if not sessions:
        return []

    session_ids = [s.id for s in sessions]

    # Active enrolled student count in this class
    active_students_count = (
        db.query(func.count(Enrollment.id))
        .filter(Enrollment.class_id == clazz.id, Enrollment.status == "ACTIVE")
        .scalar()
        or 0
    )

    # Number of marked attendance records per session (students only)
    marked_counts_query = (
        db.query(
            Attendance.class_session_id,
            func.count(Attendance.id).label("marked_count"),
        )
        .filter(
            Attendance.class_session_id.in_(session_ids),
            Attendance.student_id.isnot(None),
        )
        .group_by(Attendance.class_session_id)
        .all()
    )
    marked_map: dict[uuid.UUID, int] = {
        row.class_session_id: row.marked_count for row in marked_counts_query
    }

    # Trainer names map
    trainer_ids = {s.trainer_id for s in sessions}
    trainers = db.query(User.id, User.full_name).filter(User.id.in_(trainer_ids)).all()
    trainer_name_map: dict[uuid.UUID, str] = {t.id: t.full_name for t in trainers}

    result: list[ClassSessionRead] = []
    for s in sessions:
        dto = ClassSessionRead(
            id=s.id,
            class_id=s.class_id,
            schedule_id=s.schedule_id,
            date=s.date,
            start_at=s.start_at,
            end_at=s.end_at,
            trainer_id=s.trainer_id,
            trainer_name=trainer_name_map.get(s.trainer_id),
            status=s.status,
            note=s.note,
            student_count=active_students_count,
            marked_count=marked_map.get(s.id, 0),
        )
        result.append(dto)
    return result


def create_ad_hoc_session(
    db: Session,
    clazz: Class,
    payload: SessionCreate,
    actor: User,
) -> ClassSessionRead:
    """Create an ad-hoc session (unscheduled occurrence, schedule_id is NULL).

    Raises:
        HolidayBlocked: if the session date falls on a holiday (system or class).
        DomainError: if trainer is not allowed or slot collision occurs.
    """
    if is_holiday(db, payload.date, clazz.id):
        holiday = get_holiday(db, payload.date, clazz.id)
        if holiday:
            raise HolidayBlocked(payload.date, holiday)

    assigned_trainer_id = payload.trainer_id if payload.trainer_id is not None else clazz.trainer_id
    if not _is_trainer_allowed_for_class(db, clazz, assigned_trainer_id):
        raise DomainError("Assigned trainer is not a primary or co-trainer of this class.")

    start_at = datetime.combine(payload.date, payload.start_time, tzinfo=VIETNAM_TZ)
    end_at = datetime.combine(payload.date, payload.end_time, tzinfo=VIETNAM_TZ)

    # Check for slot collision: UNIQUE(class_id, date, start_at)
    existing_slot = (
        db.query(ClassSession)
        .filter(
            ClassSession.class_id == clazz.id,
            ClassSession.date == payload.date,
            ClassSession.start_at == start_at,
        )
        .first()
    )
    if existing_slot is not None:
        raise DomainError("A session already exists at this date and start time.")

    session = ClassSession(
        class_id=clazz.id,
        schedule_id=None,
        date=payload.date,
        start_at=start_at,
        end_at=end_at,
        trainer_id=assigned_trainer_id,
        status="scheduled",
        note=payload.note,
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DomainError("A session already exists at this date and start time.") from exc

    db.refresh(session)

    # Active student count
    active_students_count = (
        db.query(func.count(Enrollment.id))
        .filter(Enrollment.class_id == clazz.id, Enrollment.status == "ACTIVE")
        .scalar()
        or 0
    )

    trainer_user = db.query(User).filter(User.id == assigned_trainer_id).first()
    trainer_name = trainer_user.full_name if trainer_user else None

    return ClassSessionRead(
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
        student_count=active_students_count,
        marked_count=0,
    )
