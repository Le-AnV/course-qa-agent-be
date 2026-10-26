"""Contract tests for the Gemini multi-key registry (TDD)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.services import gemini_key_service as keys
from app.services.llm import gateway

TEST_DB_URL = "sqlite://"
NOW = datetime(2026, 10, 26, 20, 15, 0, tzinfo=UTC)
MODEL = "models/gemini-3.5-flash-lite"


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


class _FakeModels:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list(self):
        return [SimpleNamespace(name=name) for name in self._names]


class _FakeClient:
    def __init__(self, api_key: str | None = None, **_: object) -> None:
        if api_key == "bad-key":
            raise RuntimeError("API key not valid")
        self.api_key = api_key
        self.names = [MODEL, "models/gemini-embedding-2"]

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    @property
    def models(self):
        return _FakeModels(self.names)


@pytest.fixture(autouse=True)
def fake_genai(monkeypatch):
    monkeypatch.setattr(keys.genai, "Client", _FakeClient)


def test_create_key_encrypts_masks_and_records_models(db_session):
    raw = "AIzaSyExampleSecret1234"

    key = keys.create_key(
        db_session,
        name="Personal",
        api_key=raw,
        account_label="trung@gmail.com",
        project_label="course-qa-dev",
    )

    assert key.encrypted_key != raw
    assert raw not in key.encrypted_key
    assert key.key_last4 == "1234"
    assert key.key_fingerprint == hashlib.sha256(raw.encode()).hexdigest()
    assert key.available_models == [MODEL, "models/gemini-embedding-2"]
    assert keys.decrypt_key(key) == raw
    assert keys.masked_key(key) == "AIza...1234"


def test_create_key_rejects_a_duplicate_fingerprint(db_session):
    keys.create_key(db_session, name="Primary", api_key="AIzaSySameKey9999")

    with pytest.raises(keys.GeminiKeyError, match="already exists"):
        keys.create_key(db_session, name="Duplicate", api_key="AIzaSySameKey9999")


def test_create_key_rejects_a_key_google_will_not_accept(db_session):
    with pytest.raises(keys.GeminiKeyError, match="rejected"):
        keys.create_key(db_session, name="Broken", api_key="bad-key")


def test_select_key_skips_paused_cooldown_and_unsupported_models(db_session):
    paused = keys.create_key(db_session, name="Paused", api_key="AIzaSyPaused1111")
    keys.set_status(db_session, paused, keys.STATUS_PAUSED)
    cooling = keys.create_key(db_session, name="Cooling", api_key="AIzaSyCooling2222")
    keys.mark_key_failure(db_session, cooling, "429 RESOURCE_EXHAUSTED", now=NOW)
    unsupported = keys.create_key(db_session, name="EmbeddingOnly", api_key="AIzaSyEmbed3333")
    unsupported.available_models = ["models/gemini-embedding-2"]
    db_session.commit()
    healthy = keys.create_key(db_session, name="Healthy", api_key="AIzaSyHealthy4444")

    selected = keys.select_key(db_session, model=MODEL, now=NOW)

    assert selected is not None
    assert selected.id == healthy.id


def test_select_key_prefers_priority_then_least_recently_used(db_session):
    low = keys.create_key(db_session, name="Low", api_key="AIzaSyLow5555", priority=1)
    high = keys.create_key(db_session, name="High", api_key="AIzaSyHigh6666", priority=10)
    keys.touch_key(db_session, high, now=NOW)

    first = keys.select_key(db_session, model=MODEL, now=NOW)
    second = keys.select_key(db_session, model=MODEL, now=NOW, exclude_ids={first.id})

    assert first.id == low.id
    assert second.id == high.id


def test_mark_key_failure_cools_down_a_rate_limited_key(db_session):
    key = keys.create_key(db_session, name="Limited", api_key="AIzaSyLimit8888")

    keys.mark_key_failure(
        db_session,
        key,
        "429 RESOURCE_EXHAUSTED: requests per minute limit exceeded. retry in 30s.",
        now=NOW,
    )

    assert key.cooldown_until is not None
    assert key.cooldown_until > NOW
    assert key.status == keys.STATUS_ACTIVE
    assert "429" in key.last_error


def test_mark_key_failure_disables_an_invalid_key(db_session):
    key = keys.create_key(db_session, name="Invalid", api_key="AIzaSyInvalid9999")

    keys.mark_key_failure(
        db_session,
        key,
        "400 INVALID_ARGUMENT: API key not valid. Please pass a valid API key.",
        now=NOW,
    )

    assert key.status == keys.STATUS_INVALID
    assert "not valid" in key.last_error.lower()


def test_available_chat_models_excludes_non_chat_models(db_session):
    key = keys.create_key(db_session, name="Mixed", api_key="AIzaSyMixed7777")
    key.available_models = [
        "models/gemini-2.5-flash",
        "models/gemini-3.5-flash-lite",
        "models/gemini-embedding-2",
        "models/gemini-2.5-flash-image",
        "models/gemini-2.5-computer-use-preview-10-2025",
        "models/gemini-3.5-transcribe",
        "models/gemini-robotics-er-2-preview",
        "models/gemini-nano-banana-2.1",
    ]
    db_session.commit()

    chat_models = keys.available_chat_models(db_session)

    assert "models/gemini-2.5-flash" in chat_models
    assert "models/gemini-3.5-flash-lite" in chat_models
    assert "models/gemini-embedding-2" not in chat_models
    assert "models/gemini-2.5-flash-image" not in chat_models
    assert "models/gemini-2.5-computer-use-preview-10-2025" not in chat_models
    assert "models/gemini-3.5-transcribe" not in chat_models
    assert "models/gemini-robotics-er-2-preview" not in chat_models
    assert "models/gemini-nano-banana-2.1" not in chat_models


def test_auto_switch_defaults_on_and_can_be_disabled(db_session):
    assert keys.get_auto_switch(db_session) is True

    assert keys.set_auto_switch(db_session, False) is False
    assert keys.get_auto_switch(db_session) is False

    assert keys.set_auto_switch(db_session, True) is True
    assert keys.get_auto_switch(db_session) is True


def test_env_key_is_used_only_when_no_database_key_is_available(db_session, monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", "env-key-1234")
    env = keys.resolve_key(db_session, model=MODEL, now=NOW)
    assert env is not None
    assert env.api_key == "env-key-1234"
    assert env.key_id is None

    stored = keys.create_key(db_session, name="Stored", api_key="AIzaSyStored0000")
    resolved = keys.resolve_key(db_session, model=MODEL, now=NOW)
    assert resolved is not None
    assert resolved.key_id == stored.id
    assert resolved.api_key == "AIzaSyStored0000"

async def _collect_stream(stream) -> list[str]:
    return [chunk async for chunk in stream]


def test_gateway_fails_over_to_the_next_key_after_a_rate_limit(db_session, monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", None)
    first = keys.create_key(db_session, name="First", api_key="AIzaSyFirst1111", priority=1)
    second = keys.create_key(db_session, name="Second", api_key="AIzaSySecond2222", priority=2)
    calls: list[str] = []

    class FakeModel:
        def __init__(self, api_key: str, model: str) -> None:
            self.api_key = api_key
            self.model = model

        async def astream(self, messages):
            calls.append(self.api_key)
            if self.api_key == "AIzaSyFirst1111":
                raise RuntimeError("429 RESOURCE_EXHAUSTED: per minute limit exceeded")
            yield SimpleNamespace(content="answer")

    monkeypatch.setattr(
        gateway,
        "get_llm_model",
        lambda *, model, api_key, temperature=None: FakeModel(api_key, model),
    )

    chunks = asyncio.run(_collect_stream(gateway.astream_chat(db_session, ["hi"], model=MODEL)))

    assert chunks == ["answer"]
    assert calls == ["AIzaSyFirst1111", "AIzaSySecond2222"]
    assert first.cooldown_until is not None
    assert second.last_used_at is not None


def test_gateway_stops_after_the_first_key_when_auto_switch_is_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", None)
    keys.set_auto_switch(db_session, False)
    keys.create_key(db_session, name="First", api_key="AIzaSyFirst1111", priority=1)
    keys.create_key(db_session, name="Second", api_key="AIzaSySecond2222", priority=2)
    calls: list[str] = []

    class FakeModel:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        async def astream(self, messages):
            calls.append(self.api_key)
            raise RuntimeError("429 RESOURCE_EXHAUSTED: per minute limit exceeded")
            yield  # pragma: no cover

    monkeypatch.setattr(
        gateway,
        "get_llm_model",
        lambda *, model, api_key, temperature=None: FakeModel(api_key),
    )

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(_collect_stream(gateway.astream_chat(db_session, ["hi"], model=MODEL)))

    assert calls == ["AIzaSyFirst1111"]


def test_gateway_restarts_on_the_next_key_after_a_mid_stream_limit(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "google_api_key", None)
    first = keys.create_key(
        db_session, name="Partial", api_key="AIzaSyPartial3333", priority=1
    )
    second = keys.create_key(
        db_session, name="Recovery", api_key="AIzaSyRecovery4444", priority=2
    )
    calls: list[str] = []

    class FakeModel:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        async def astream(self, messages):
            calls.append(self.api_key)
            if self.api_key == "AIzaSyPartial3333":
                yield SimpleNamespace(content="partial")
                raise RuntimeError("429 RESOURCE_EXHAUSTED: per minute limit exceeded")
            yield SimpleNamespace(content="complete answer")

    monkeypatch.setattr(
        gateway,
        "get_llm_model",
        lambda *, model, api_key, temperature=None: FakeModel(api_key),
    )

    chunks = asyncio.run(
        _collect_stream(gateway.astream_chat(db_session, ["hi"], model=MODEL))
    )

    assert chunks[0] == "partial"
    assert isinstance(chunks[1], gateway.StreamRestart)
    assert chunks[2:] == ["complete answer"]
    assert calls == ["AIzaSyPartial3333", "AIzaSyRecovery4444"]
    assert first.cooldown_until is not None
    assert second.last_used_at is not None


def test_gateway_raises_mid_stream_when_auto_switch_is_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", None)
    keys.set_auto_switch(db_session, False)
    keys.create_key(db_session, name="Partial", api_key="AIzaSyPartial3333", priority=1)
    keys.create_key(db_session, name="Unused", api_key="AIzaSyUnused4444", priority=2)
    calls: list[str] = []

    class FakeModel:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        async def astream(self, messages):
            calls.append(self.api_key)
            yield SimpleNamespace(content="partial")
            raise RuntimeError("429 RESOURCE_EXHAUSTED: per minute limit exceeded")

    monkeypatch.setattr(
        gateway,
        "get_llm_model",
        lambda *, model, api_key, temperature=None: FakeModel(api_key),
    )

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(
            _collect_stream(gateway.astream_chat(db_session, ["hi"], model=MODEL))
        )

    assert calls == ["AIzaSyPartial3333"]
