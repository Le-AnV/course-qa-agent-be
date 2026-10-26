"""Contract tests for the OpenAI decisions key pool (TDD)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.services import openai_key_service as keys

TEST_DB_URL = "sqlite://"
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
API_KEY = "sk-test-abcdef1234"


@pytest.fixture()
def db_session(monkeypatch):
    from app.core import database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)
    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dbmod._db_url = TEST_DB_URL
    Base.metadata.create_all(bind=engine)
    SessionLocal = get_session_local(TEST_DB_URL)
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
    reset_engine()


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)

    def _valid(api_key: str) -> list[str]:
        if api_key == "bad-key":
            raise keys.OpenAIApiKeyError("OpenAI rejected this API key.")
        return ["gpt-6-luna"]

    monkeypatch.setattr(keys, "validate_key", _valid)


def test_create_key_stores_masked_value_and_never_the_raw_key(db_session) -> None:
    key = keys.create_key(db_session, name="Decisions 1", api_key=API_KEY)

    assert key.status == keys.STATUS_ACTIVE
    assert keys.masked_key(key) == "sk-t...1234"
    assert key.encrypted_key != API_KEY
    assert keys.decrypt_key(key) == API_KEY


def test_create_key_rejects_a_duplicate_key(db_session) -> None:
    keys.create_key(db_session, name="First", api_key=API_KEY)

    with pytest.raises(keys.OpenAIApiKeyError, match="already exists"):
        keys.create_key(db_session, name="Second", api_key=API_KEY)


def test_create_key_rejects_a_key_openai_refuses(db_session) -> None:
    with pytest.raises(keys.OpenAIApiKeyError, match="rejected"):
        keys.create_key(db_session, name="Bad", api_key="bad-key")


def test_select_key_skips_paused_and_cooling_keys(db_session) -> None:
    cooling = keys.create_key(db_session, name="Cooling", api_key="sk-cool-0001", priority=1)
    paused = keys.create_key(db_session, name="Paused", api_key="sk-paus-0002", priority=2)
    healthy = keys.create_key(db_session, name="Healthy", api_key="sk-heal-0003", priority=3)
    keys.mark_key_failure(db_session, cooling, "429 Too Many Requests", now=NOW)
    keys.set_status(db_session, paused, keys.STATUS_PAUSED)

    chosen = keys.select_key(db_session, now=NOW)

    assert chosen is not None
    assert chosen.id == healthy.id


def test_mark_key_failure_disables_a_rejected_key(db_session) -> None:
    key = keys.create_key(db_session, name="Revoked", api_key=API_KEY)

    keys.mark_key_failure(db_session, key, "401 Unauthorized", now=NOW)

    db_session.refresh(key)
    assert key.status == keys.STATUS_INVALID
    assert keys.effective_status(key, now=NOW) == keys.STATUS_INVALID


def test_mark_key_failure_cools_down_a_rate_limited_key(db_session) -> None:
    key = keys.create_key(db_session, name="Limited", api_key=API_KEY)

    keys.mark_key_failure(db_session, key, "429 Too Many Requests", now=NOW)

    db_session.refresh(key)
    assert key.status == keys.STATUS_ACTIVE
    assert key.cooldown_until is not None
    assert keys.effective_status(key, now=NOW) == keys.STATUS_COOLDOWN
    assert keys.effective_status(key, now=NOW + timedelta(seconds=61)) == keys.STATUS_ACTIVE


def test_resolve_key_falls_back_to_the_environment_key(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "env-key")

    resolved = keys.resolve_key(db_session)

    assert resolved is not None
    assert resolved.api_key == "env-key"
    assert resolved.key_id is None
    assert resolved.key_name == "Environment"


def test_resolve_key_prefers_a_stored_key_over_the_environment(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "env-key")
    stored = keys.create_key(db_session, name="Stored", api_key=API_KEY)

    resolved = keys.resolve_key(db_session)

    assert resolved is not None
    assert resolved.key_id == stored.id
    assert resolved.api_key == API_KEY


def test_resolve_key_returns_none_without_pool_or_environment(db_session) -> None:
    assert keys.resolve_key(db_session) is None
