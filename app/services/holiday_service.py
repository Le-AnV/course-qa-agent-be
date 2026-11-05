"""Shared holiday service (AI-156).

The ``holiday`` table from AI-155 is the source of truth:

* ``scope='system'`` rows apply to every class.
* ``scope='class'`` rows apply to one class only.

Dates are matched in Vietnam local time (UTC+7). The schema stores date-only
holidays (AI-155 C3), so a holiday suppresses the whole local day.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.attendance import Holiday
from app.models.system_config import SystemConfig
from app.services.vietnamese_lunar import lunar_date_to_solar

logger = logging.getLogger(__name__)

VIETNAM_TZ = timezone(timedelta(hours=7))
SEED_MARKER_KEY = "holiday_seed_through_year"

# Fixed solar holidays from Jira AI-156 AC-02. Admin edits (AI-140) happen in
# the table, so a deleted row is not silently re-created by a later seed.
SOLAR_HOLIDAYS: tuple[tuple[int, int, str], ...] = (
    (1, 1, "Tết Dương lịch"),
    (3, 8, "Quốc tế Phụ nữ"),
    (4, 30, "Ngày Giải phóng miền Nam"),
    (5, 1, "Quốc tế Lao động"),
    (9, 2, "Quốc khánh"),
    (10, 20, "Ngày Phụ nữ Việt Nam"),
    (11, 20, "Ngày Nhà giáo Việt Nam"),
)

# Lunar holidays from Jira AI-156 AC-03, expressed as lunar (month, day).
LUNAR_HOLIDAYS: tuple[tuple[int, int, str], ...] = (
    (1, 1, "Tết Nguyên Đán"),
    (3, 10, "Giỗ Tổ Hùng Vương"),
)


class HolidayBlocked(Exception):
    """Raised when a workflow requires a working day but lands on a holiday."""

    def __init__(self, day: date, holiday: Holiday) -> None:
        self.day = day
        self.holiday = holiday
        super().__init__(f"{day.isoformat()} is a holiday: {holiday.name}")


def local_date(moment: datetime) -> date:
    """Return the Vietnam-local date for an aware (or assumed-UTC) timestamp."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(VIETNAM_TZ).date()


def get_holiday(
    db: Session,
    day: date,
    class_id: uuid.UUID | None = None,
) -> Holiday | None:
    """Return the most specific holiday for ``day``, if any.

    A class-scoped row wins over a system row on the same date so the class
    calendar can carry its own label. Passing ``class_id=None`` only matches
    system holidays.
    """
    if class_id is not None:
        row = (
            db.query(Holiday)
            .filter(
                Holiday.date == day,
                Holiday.scope == "class",
                Holiday.class_id == class_id,
            )
            .first()
        )
        if row is not None:
            return row
    return db.query(Holiday).filter(Holiday.date == day, Holiday.scope == "system").first()


def is_holiday(
    db: Session,
    day: date,
    class_id: uuid.UUID | None = None,
) -> bool:
    """Return whether ``day`` is a holiday for the class (or system-wide)."""
    return get_holiday(db, day, class_id) is not None


def holidays_between(
    db: Session,
    start: date,
    end: date,
    class_id: uuid.UUID | None = None,
) -> list[Holiday]:
    """Return holidays in ``[start, end]`` after applying class scope."""
    query = db.query(Holiday).filter(Holiday.date >= start, Holiday.date <= end)
    if class_id is None:
        query = query.filter(Holiday.scope == "system")
    else:
        query = query.filter(
            or_(
                Holiday.scope == "system",
                and_(Holiday.scope == "class", Holiday.class_id == class_id),
            )
        )
    return query.order_by(Holiday.date.asc()).all()


def holiday_dates(
    db: Session,
    start: date,
    end: date,
    class_id: uuid.UUID | None = None,
) -> set[date]:
    """Return the set of holiday dates in ``[start, end]`` for ``class_id``."""
    return {row.date for row in holidays_between(db, start, end, class_id)}


def ensure_working_day(
    db: Session,
    day: date,
    class_id: uuid.UUID | None = None,
) -> None:
    """Raise :class:`HolidayBlocked` when ``day`` is a holiday.

    Session creation (AI-136), absence counting and ticket handover (AI-158)
    call this shared guard instead of duplicating the query.
    """
    holiday = get_holiday(db, day, class_id)
    if holiday is not None:
        raise HolidayBlocked(day, holiday)


def ensure_year_holidays(db: Session, year: int) -> int:
    """Insert the default system holidays for ``year``; return how many are new.

    The operation is insert-only: existing rows are never renamed or moved, so
    edits made through the AI-140 admin CRUD survive a restart. The caller owns
    the commit.
    """
    first = date(year, 1, 1)
    last = date(year, 12, 31)
    existing = {
        row[0]
        for row in db.query(Holiday.date)
        .filter(
            Holiday.date >= first,
            Holiday.date <= last,
            Holiday.scope == "system",
        )
        .all()
    }

    created = 0
    for month, day, name in SOLAR_HOLIDAYS:
        created += _add_system_holiday(db, existing, date(year, month, day), name, "solar")

    for month, day, name in LUNAR_HOLIDAYS:
        try:
            solar = lunar_date_to_solar(year, month, day)
        except (TypeError, ValueError) as exc:
            logger.warning("Cannot resolve lunar holiday %s %s/%s: %s", name, day, month, exc)
            continue
        created += _add_system_holiday(db, existing, solar, name, "lunar")

    if created:
        db.flush()
    return created


def _add_system_holiday(
    db: Session,
    existing: set[date],
    day: date,
    name: str,
    holiday_type: str,
) -> int:
    if day in existing:
        return 0
    db.add(Holiday(date=day, name=name, type=holiday_type, scope="system"))
    existing.add(day)
    return 1


def seed_holidays(
    db: Session,
    years: Iterable[int] | None = None,
    *,
    today: date | None = None,
) -> int:
    """Seed default holidays for ``years`` (default: current and next year).

    A ``system_config`` marker records the highest year whose defaults were
    seeded. That keeps a restart from resurrecting a holiday an admin deleted
    through AI-140 while still rolling the defaults into the next year.
    """
    current = (today or local_date(datetime.now(UTC))).year
    marker = _seed_marker(db)

    if years is None:
        target = {current, current + 1}
        if marker is not None and marker < current:
            target.update(range(marker + 1, current + 1))
        if marker is not None and marker >= max(target):
            return 0
    else:
        target = {int(year) for year in years}

    created = sum(ensure_year_holidays(db, year) for year in sorted(target))
    _write_seed_marker(db, max(target))
    db.commit()
    return created


def _seed_marker(db: Session) -> int | None:
    row = db.get(SystemConfig, SEED_MARKER_KEY)
    if row is None:
        return None
    try:
        return int(row.value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s value: %r", SEED_MARKER_KEY, row.value)
        return None


def _write_seed_marker(db: Session, year: int) -> None:
    row = db.get(SystemConfig, SEED_MARKER_KEY)
    if row is None:
        db.add(SystemConfig(key=SEED_MARKER_KEY, value=str(year)))
        return
    row.value = str(year)


def business_elapsed(
    start: datetime,
    end: datetime,
    holidays: Iterable[date],
) -> timedelta:
    """Return elapsed wall-clock time after removing full Vietnam holiday days."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    start_local = start.astimezone(VIETNAM_TZ)
    end_local = end.astimezone(VIETNAM_TZ)
    if end_local <= start_local:
        return timedelta(0)

    elapsed = end_local - start_local
    for day in set(holidays):
        day_start = datetime.combine(day, time.min, tzinfo=VIETNAM_TZ)
        day_end = day_start + timedelta(days=1)
        overlap_start = max(start_local, day_start)
        overlap_end = min(end_local, day_end)
        if overlap_end > overlap_start:
            elapsed -= overlap_end - overlap_start

    return max(elapsed, timedelta(0))
