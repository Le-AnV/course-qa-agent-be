"""Daily chat question quota for students.

The counter lives in ``chat_daily_usage`` keyed by (user, local day) so deleting
a conversation cannot reset the quota. Increments are atomic in the database,
which keeps two open tabs from spending the same last question twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.usage import ChatDailyUsage
from app.models.user import User

QUOTA_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
STUDENT_ROLE = "STUDENT"


@dataclass(frozen=True)
class QuotaStatus:
    """Remaining allowance for the current local day.

    ``limit``/``remaining``/``resets_at`` are ``None`` for unrestricted roles.
    """

    limit: int | None
    used: int
    remaining: int | None
    resets_at: datetime | None
    day: date | None
    exhausted: bool = False


def _today() -> date:
    return datetime.now(QUOTA_TIMEZONE).date()


def _reset_at(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=QUOTA_TIMEZONE)


def _is_student(user: User) -> bool:
    return user.role is not None and user.role.name == STUDENT_ROLE


def _status(day: date, used: int, *, exhausted: bool = False) -> QuotaStatus:
    limit = settings.student_daily_question_limit
    return QuotaStatus(
        limit=limit,
        used=used,
        remaining=max(limit - used, 0),
        resets_at=_reset_at(day),
        day=day,
        exhausted=exhausted,
    )


def _used(db: Session, user_id: uuid.UUID, day: date) -> int:
    value = db.scalar(
        select(ChatDailyUsage.used).where(
            ChatDailyUsage.user_id == user_id,
            ChatDailyUsage.usage_date == day,
        )
    )
    return int(value or 0)


def get_status(db: Session, user: User) -> QuotaStatus:
    """Read the allowance without spending a question."""

    if not _is_student(user):
        return QuotaStatus(limit=None, used=0, remaining=None, resets_at=None, day=None)

    day = _today()
    used = _used(db, user.id, day)
    return _status(day, used, exhausted=used >= settings.student_daily_question_limit)


def consume_question(db: Session, user: User) -> QuotaStatus:
    """Atomically spend one question for a student.

    Returns a status with ``exhausted=True`` when the limit is already reached.
    Unrestricted roles get an unlimited status without touching the table.
    """

    if not _is_student(user):
        return QuotaStatus(limit=None, used=0, remaining=None, resets_at=None, day=None)

    limit = settings.student_daily_question_limit
    day = _today()

    spent = db.execute(
        update(ChatDailyUsage)
        .where(
            ChatDailyUsage.user_id == user.id,
            ChatDailyUsage.usage_date == day,
            ChatDailyUsage.used < limit,
        )
        .values(used=ChatDailyUsage.used + 1)
    )
    if spent.rowcount:
        db.commit()
        return _status(day, _used(db, user.id, day))

    used = _used(db, user.id, day)
    if used:
        db.rollback()
        return _status(day, used, exhausted=True)

    # No counter for today yet. The insert is the only race here: when a
    # concurrent request wins, fall through to the same atomic update.
    try:
        db.add(ChatDailyUsage(user_id=user.id, usage_date=day, used=1))
        db.commit()
        return _status(day, 1)
    except IntegrityError:
        db.rollback()

    spent = db.execute(
        update(ChatDailyUsage)
        .where(
            ChatDailyUsage.user_id == user.id,
            ChatDailyUsage.usage_date == day,
            ChatDailyUsage.used < limit,
        )
        .values(used=ChatDailyUsage.used + 1)
    )
    db.commit()
    used = _used(db, user.id, day)
    return _status(day, used, exhausted=not spent.rowcount)


def refund_question(db: Session, user_id: uuid.UUID, day: date) -> None:
    """Give a question back when the answer failed before completion."""

    db.execute(
        update(ChatDailyUsage)
        .where(
            ChatDailyUsage.user_id == user_id,
            ChatDailyUsage.usage_date == day,
            ChatDailyUsage.used > 0,
        )
        .values(used=ChatDailyUsage.used - 1)
    )
    db.commit()


def limit_reached_message(status: QuotaStatus) -> str:
    resets = status.resets_at.strftime("%H:%M") if status.resets_at is not None else "tomorrow"
    return (
        f"Daily question limit reached ({status.limit}/{status.limit}). "
        f"Your quota resets at {resets}."
    )
