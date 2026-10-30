"""AI-155: attendance / schedule / holiday / handover schema.

Schema-level guarantees only â€” the session generator and the API land with
AI-136/AI-157/AI-158. Every test runs on SQLite through ``create_all``, so the
CHECK constraints and partial unique indexes must be portable SQL.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (register tables)
from app.core.database import Base
from app.models.attendance import (
    Attendance,
    ClassSchedule,
    ClassSession,
    Holiday,
    TicketHandover,
)


@pytest.fixture()
def db_session():
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


def _schedule(db_session, **overrides):
    schedule = ClassSchedule(
        **{
            "class_id": uuid.uuid4(),
            "subject": "C++ Programming",
            "weekday": 0,
            "start_time": time(19, 0),
            "end_time": time(21, 0),
            "trainer_id": uuid.uuid4(),
            "effective_from": date(2026, 11, 2),
            **overrides,
        }
    )
    db_session.add(schedule)
    db_session.flush()
    return schedule


def _session(db_session, **overrides):
    start = datetime(2026, 11, 2, 19, 0, tzinfo=UTC)
    values = {
        "class_id": uuid.uuid4(),
        "schedule_id": None,
        "date": start.date(),
        "start_at": start,
        "end_at": start + timedelta(hours=2),
        "trainer_id": uuid.uuid4(),
        **overrides,
    }
    session = ClassSession(**values)
    db_session.add(session)
    db_session.flush()
    return session


def test_schema_creates_the_five_new_tables(db_session):
    tables = set(inspect(db_session.get_bind()).get_table_names())

    assert {
        "class_schedule",
        "class_session",
        "attendance",
        "holiday",
        "ticket_handover",
    } <= tables


def test_class_schedule_rejects_end_before_start(db_session):
    with pytest.raises(IntegrityError):
        _schedule(db_session, end_time=time(18, 0))
        db_session.commit()


def test_class_schedule_rejects_weekday_out_of_range(db_session):
    with pytest.raises(IntegrityError):
        _schedule(db_session, weekday=7)
        db_session.commit()


def test_class_session_slot_is_unique(db_session):
    slot = _session(db_session)
    db_session.commit()

    with pytest.raises(IntegrityError):
        _session(
            db_session,
            class_id=slot.class_id,
            date=slot.date,
            start_at=slot.start_at,
            end_at=slot.end_at,
        )
        db_session.commit()


def test_cancelled_session_keeps_its_slot_reserved(db_session):
    slot = _session(db_session, status="cancelled")
    db_session.commit()

    with pytest.raises(IntegrityError):
        _session(
            db_session,
            class_id=slot.class_id,
            date=slot.date,
            start_at=slot.start_at,
            end_at=slot.end_at,
            status="scheduled",
        )
        db_session.commit()


def test_attendance_requires_exactly_one_person(db_session):
    session = _session(db_session)

    for student_id, trainer_id in ((None, None), (uuid.uuid4(), uuid.uuid4())):
        db_session.add(
            Attendance(
                class_session_id=session.id,
                student_id=student_id,
                trainer_id=trainer_id,
                status="present",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


def test_attendance_allows_one_student_and_one_trainer_per_session(db_session):
    session = _session(db_session)
    student_id = uuid.uuid4()
    trainer_id = uuid.uuid4()
    db_session.add_all(
        [
            Attendance(
                class_session_id=session.id,
                student_id=student_id,
                trainer_id=None,
                status="late",
            ),
            Attendance(
                class_session_id=session.id,
                student_id=None,
                trainer_id=trainer_id,
                status="present",
            ),
        ]
    )

    db_session.commit()

    assert db_session.query(Attendance).count() == 2


def test_attendance_rejects_duplicate_student_in_one_session(db_session):
    session = _session(db_session)
    student_id = uuid.uuid4()
    db_session.add(
        Attendance(class_session_id=session.id, student_id=student_id, status="present")
    )
    db_session.commit()

    db_session.add(
        Attendance(class_session_id=session.id, student_id=student_id, status="excused_absence")
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_attendance_rejects_unknown_status(db_session):
    session = _session(db_session)
    db_session.add(
        Attendance(class_session_id=session.id, student_id=uuid.uuid4(), status="absent")
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_holiday_scope_must_match_class_id(db_session):
    db_session.add(Holiday(date=date(2026, 11, 5), name="X", type="solar", scope="class"))

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        Holiday(
            date=date(2026, 11, 5),
            name="X",
            type="solar",
            scope="system",
            class_id=uuid.uuid4(),
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_system_holiday_date_is_unique_but_class_holidays_do_not_collide(db_session):
    db_session.add(Holiday(date=date(2026, 11, 5), name="Lunar festival", type="lunar", scope="system"))
    first_class = uuid.uuid4()
    second_class = uuid.uuid4()
    db_session.add_all(
        [
            Holiday(
                date=date(2026, 11, 5),
                name="Trip",
                type="admin",
                scope="class",
                class_id=first_class,
            ),
            Holiday(
                date=date(2026, 11, 5),
                name="Trip",
                type="admin",
                scope="class",
                class_id=second_class,
            ),
        ]
    )
    db_session.commit()

    db_session.add(Holiday(date=date(2026, 11, 5), name="Duplicate", type="solar", scope="system"))

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_ticket_handover_rejects_the_same_trainer_twice(db_session):
    trainer_id = uuid.uuid4()
    db_session.add(
        TicketHandover(
            ticket_id=uuid.uuid4(),
            from_trainer_id=trainer_id,
            to_trainer_id=trainer_id,
            reason="manual_transfer",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_ticket_handover_allows_only_one_pending_owner(db_session):
    ticket_id = uuid.uuid4()
    db_session.add(
        TicketHandover(
            ticket_id=ticket_id,
            from_trainer_id=uuid.uuid4(),
            to_trainer_id=uuid.uuid4(),
            reason="auto_absent",
        )
    )
    db_session.commit()

    db_session.add(
        TicketHandover(
            ticket_id=ticket_id,
            from_trainer_id=uuid.uuid4(),
            to_trainer_id=uuid.uuid4(),
            reason="manual_transfer",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_ticket_handover_allows_a_new_pending_after_rejection(db_session):
    ticket_id = uuid.uuid4()
    db_session.add(
        TicketHandover(
            ticket_id=ticket_id,
            from_trainer_id=uuid.uuid4(),
            to_trainer_id=uuid.uuid4(),
            reason="auto_absent",
            status="rejected",
        )
    )
    db_session.commit()

    db_session.add(
        TicketHandover(
            ticket_id=ticket_id,
            from_trainer_id=uuid.uuid4(),
            to_trainer_id=uuid.uuid4(),
            reason="manual_transfer",
        )
    )

    db_session.commit()

    assert db_session.query(TicketHandover).filter_by(ticket_id=ticket_id).count() == 2
