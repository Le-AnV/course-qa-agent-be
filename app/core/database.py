"""Shared SQLAlchemy base + engine/session factory."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None
_db_url: str | None = None


def build_engine(database_url: str):
    """Sync engine. SQLite (tests/local) needs check_same_thread=False."""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


def init_engine(database_url: str):
    """Bind the module engine/SessionLocal to a URL (idempotent per URL)."""
    global _engine, _SessionLocal, _db_url
    if _engine is None or _db_url != database_url:
        _engine = build_engine(database_url)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
        _db_url = database_url
    return _engine


def reset_engine() -> None:
    """Test-only: dispose pooled connections and drop the cached engine."""
    global _engine, _SessionLocal, _db_url
    if _engine is not None:
        _engine.dispose()  # release pooled SQLite handles (Windows file lock)
    _engine = None
    _SessionLocal = None
    _db_url = None


def get_session_local(database_url: str) -> sessionmaker:
    init_engine(database_url)
    assert _SessionLocal is not None
    return _SessionLocal


def get_engine():
    return _engine


def create_all_tables(database_url: str | None = None) -> None:
    """Create all tables. Import models first so metadata is populated."""
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    if database_url is not None:
        init_engine(database_url)
    assert _engine is not None, "engine not initialised — pass database_url first"
    Base.metadata.create_all(bind=_engine)
