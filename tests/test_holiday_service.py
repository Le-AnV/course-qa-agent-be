"""AI-156: shared holiday service and Vietnamese lunar seed."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (register tables)
from app.core.database import Base
from app.models.attendance import Holiday
from app.models.system_config import SystemConfig
from app.services.holiday_service import (
    SOLAR_HOLIDAYS,
    HolidayBlocked,
    business_elapsed,
    ensure_working_day,
    ensure_year_holidays,
    get_holiday,
    holidays_between,
    is_holiday,
    local_date,
    seed_holidays,
)
from app.services.vietnamese_lunar import hung_kings_date, tet_date

SYSTEM_HOLIDAY_COUNT = len(SOLAR_HOLIDAYS) + 2


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


@pytest.mark.parametrize(("month", "day", "name"), SOLAR_HOLIDAYS)
def test_seeded_solar_holidays_are_recognised(db_session, month, day, name):
    ensure_year_holidays(db_session, 2026)

    row = get_holiday(db_session, date(2026, month, day))

    assert row is not None
    assert row.name == name
    assert row.type == "solar"
    assert row.scope == "system"


def test_lunar_holidays_use_vietnamese_calendar(db_session):
    ensure_year_holidays(db_session, 2026)

    assert tet_date(2026) == date(2026, 2, 17)
    assert hung_kings_date(2026) == date(2026, 4, 26)
    assert get_holiday(db_session, date(2026, 2, 17)).name == "Tết Nguyên Đán"
    assert get_holiday(db_session, date(2026, 4, 26)).name == "Giỗ Tổ Hùng Vương"


def test_ensure_year_holidays_is_idempotent(db_session):
    first = ensure_year_holidays(db_session, 2026)
    second = ensure_year_holidays(db_session, 2026)

    assert first == SYSTEM_HOLIDAY_COUNT
    assert second == 0
    assert db_session.query(Holiday).filter(Holiday.scope == "system").count() == first


def test_seed_holidays_is_idempotent(db_session):
    first = seed_holidays(db_session, [2026])
    second = seed_holidays(db_session, [2026])

    assert first == SYSTEM_HOLIDAY_COUNT
    assert second == 0


def test_seed_defaults_to_the_current_and_next_year(db_session):
    created = seed_holidays(db_session, today=date(2026, 6, 1))

    assert created == SYSTEM_HOLIDAY_COUNT * 2
    assert get_holiday(db_session, date(2026, 9, 2)) is not None
    assert get_holiday(db_session, date(2027, 9, 2)) is not None
    assert db_session.get(SystemConfig, "holiday_seed_through_year").value == "2027"


def test_seed_does_not_overwrite_an_admin_edit(db_session):
    ensure_year_holidays(db_session, 2026)
    tet = get_holiday(db_session, date(2026, 2, 17))
    tet.name = "Tết chỉnh sửa"
    db_session.commit()

    ensure_year_holidays(db_session, 2026)

    assert get_holiday(db_session, date(2026, 2, 17)).name == "Tết chỉnh sửa"


def test_seed_marker_prevents_resurrecting_a_deleted_holiday(db_session):
    seed_holidays(db_session, today=date(2026, 6, 1))
    national_day = get_holiday(db_session, date(2026, 9, 2))
    db_session.delete(national_day)
    db_session.commit()

    seed_holidays(db_session, today=date(2026, 6, 2))

    assert get_holiday(db_session, date(2026, 9, 2)) is None


def test_class_holiday_only_applies_to_that_class(db_session):
    class_a = uuid.uuid4()
    class_b = uuid.uuid4()
    day = date(2026, 11, 5)
    db_session.add(
        Holiday(
            date=day,
            name="Class A off",
            type="admin",
            scope="class",
            class_id=class_a,
        )
    )
    db_session.commit()

    assert is_holiday(db_session, day, class_a) is True
    assert is_holiday(db_session, day, class_b) is False
    assert is_holiday(db_session, day) is False


def test_system_holiday_applies_to_every_class(db_session):
    class_a = uuid.uuid4()
    day = date(2026, 9, 2)
    db_session.add(Holiday(date=day, name="Quốc khánh", type="solar", scope="system"))
    db_session.commit()

    assert is_holiday(db_session, day, class_a) is True
    assert is_holiday(db_session, day) is True


def test_holidays_between_filters_by_class_scope(db_session):
    class_a = uuid.uuid4()
    class_b = uuid.uuid4()
    db_session.add_all(
        [
            Holiday(date=date(2026, 9, 1), name="System", type="solar", scope="system"),
            Holiday(
                date=date(2026, 9, 2),
                name="Class A",
                type="admin",
                scope="class",
                class_id=class_a,
            ),
            Holiday(
                date=date(2026, 9, 3),
                name="Class B",
                type="admin",
                scope="class",
                class_id=class_b,
            ),
        ]
    )
    db_session.commit()

    class_rows = holidays_between(db_session, date(2026, 9, 1), date(2026, 9, 5), class_a)
    system_rows = holidays_between(db_session, date(2026, 9, 1), date(2026, 9, 5))

    assert [row.date for row in class_rows] == [date(2026, 9, 1), date(2026, 9, 2)]
    assert [row.date for row in system_rows] == [date(2026, 9, 1)]


def test_ensure_working_day_raises_on_holiday(db_session):
    class_a = uuid.uuid4()
    day = date(2026, 11, 5)
    db_session.add(
        Holiday(date=day, name="Nghỉ lớp", type="admin", scope="class", class_id=class_a)
    )
    db_session.commit()

    with pytest.raises(HolidayBlocked) as exc:
        ensure_working_day(db_session, day, class_a)

    assert exc.value.day == day
    assert exc.value.holiday.name == "Nghỉ lớp"

    ensure_working_day(db_session, date(2026, 11, 6), class_a)


def test_business_elapsed_removes_a_full_holiday_day():
    start = datetime(2026, 4, 29, 10, tzinfo=UTC)
    end = datetime(2026, 5, 1, 10, tzinfo=UTC)

    assert business_elapsed(start, end, [date(2026, 4, 30)]) == timedelta(hours=24)
    assert business_elapsed(start, end, []) == timedelta(hours=48)


def test_business_elapsed_handles_a_partial_holiday():
    start = datetime(2026, 4, 29, 20, tzinfo=UTC)  # 03:00 Vietnam, Apr 30
    end = datetime(2026, 5, 1, 0, tzinfo=UTC)  # 07:00 Vietnam, May 1

    assert business_elapsed(start, end, [date(2026, 4, 30)]) == timedelta(hours=7)


def test_local_date_uses_vietnam_timezone():
    moment = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)

    assert local_date(moment) == date(2026, 11, 5)
