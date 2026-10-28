"""Daily student question quota behavior."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (register all tables)
from app.config import settings
from app.core.database import Base
from app.models.usage import ChatDailyUsage
from app.models.user import Role, User
from app.services import quota_service


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def users(db_session: Session) -> tuple[User, User]:
    student_role = Role(name="STUDENT")
    trainer_role = Role(name="TRAINER")
    db_session.add_all([student_role, trainer_role])
    db_session.flush()
    student = User(
        full_name="Student",
        email="quota-student@example.com",
        role_id=student_role.id,
        status="ACTIVE",
    )
    trainer = User(
        full_name="Trainer",
        email="quota-trainer@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    db_session.add_all([student, trainer])
    db_session.commit()
    return student, trainer


@pytest.fixture(autouse=True)
def _limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "student_daily_question_limit", 3)


def _rows(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(ChatDailyUsage)) or 0)


def test_consume_stops_at_the_daily_limit(db_session: Session, users: tuple[User, User]) -> None:
    student, _ = users

    statuses = [quota_service.consume_question(db_session, student) for _ in range(4)]

    assert [status.used for status in statuses] == [1, 2, 3, 3]
    assert statuses[-1].exhausted is True
    assert statuses[-1].remaining == 0
    assert _rows(db_session) == 1


def test_get_status_does_not_spend_a_question(db_session: Session, users: tuple[User, User]) -> None:
    student, _ = users

    first = quota_service.get_status(db_session, student)
    second = quota_service.get_status(db_session, student)

    assert first.remaining == 3
    assert second.remaining == 3
    assert _rows(db_session) == 0


def test_refund_restores_one_question(db_session: Session, users: tuple[User, User]) -> None:
    student, _ = users
    used = quota_service.consume_question(db_session, student)
    assert used.remaining == 2

    assert used.day is not None
    quota_service.refund_question(db_session, student.id, used.day)

    assert quota_service.get_status(db_session, student).remaining == 3


def test_unrestricted_roles_are_not_counted(db_session: Session, users: tuple[User, User]) -> None:
    _, trainer = users

    status = quota_service.consume_question(db_session, trainer)

    assert status.limit is None
    assert status.remaining is None
    assert status.day is None
    assert _rows(db_session) == 0
