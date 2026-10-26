"""Scope-gate tests for the decisions key pool and rotation (TDD)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.services import openai_decision_service, openai_key_service

TEST_DB_URL = "sqlite://"
OUT_OF_SCOPE = {
    "answers": [
        {
            "type": "choice",
            "name": "scope",
            "choice": "out_of_scope",
            "confidence": 0.95,
        }
    ]
}


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
    monkeypatch.setattr(
        openai_key_service, "validate_key", lambda api_key: ["gpt-6-luna"]
    )


class _FakeApi:
    """Answer the decisions endpoint with the configured status sequence."""

    def __init__(self, responses: list[tuple[int, dict | None]]) -> None:
        self.responses = list(responses)
        self.auth_headers: list[str] = []

    def client(self):
        api = self

        class FakeClient:
            def __init__(self, **_: object) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, url: str, *, headers: dict, json: dict):
                api.auth_headers.append(headers["Authorization"])
                status_code, payload = api.responses.pop(0)
                request = httpx.Request("POST", url)

                class FakeResponse:
                    status_code_ = status_code

                    def raise_for_status(self) -> None:
                        if status_code >= 400:
                            raise httpx.HTTPStatusError(
                                f"{status_code} error",
                                request=request,
                                response=httpx.Response(status_code, request=request),
                            )

                    def json(self) -> dict:
                        return payload or {}

                return FakeResponse()

        return FakeClient


def _ask(db: Session, api: _FakeApi, monkeypatch) -> bool:
    monkeypatch.setattr(openai_decision_service.httpx, "AsyncClient", api.client())
    return asyncio.run(
        openai_decision_service.is_confidently_out_of_scope(
            class_name="C++ Basic",
            class_description="Introductory C++ course",
            question="How do Python lists work?",
            history="",
            db=db,
        )
    )


def test_scope_decision_uses_the_pool_key(db_session, monkeypatch) -> None:
    openai_key_service.create_key(
        db_session, name="Pool key", api_key="sk-pool-key-1111"
    )
    api = _FakeApi([(200, OUT_OF_SCOPE)])

    result = _ask(db_session, api, monkeypatch)

    assert result is True
    assert api.auth_headers == ["Bearer sk-pool-key-1111"]


def test_scope_decision_rotates_after_a_rate_limit(db_session, monkeypatch) -> None:
    limited = openai_key_service.create_key(
        db_session, name="Limited", api_key="sk-limited-0001", priority=1
    )
    openai_key_service.create_key(
        db_session, name="Healthy", api_key="sk-healthy-0002", priority=2
    )
    api = _FakeApi([(429, None), (200, OUT_OF_SCOPE)])

    result = _ask(db_session, api, monkeypatch)

    assert result is True
    assert api.auth_headers == ["Bearer sk-limited-0001", "Bearer sk-healthy-0002"]
    db_session.refresh(limited)
    assert limited.cooldown_until is not None


def test_scope_decision_marks_a_rejected_key_invalid(db_session, monkeypatch) -> None:
    rejected = openai_key_service.create_key(
        db_session, name="Rejected", api_key="sk-rejected-0003"
    )
    api = _FakeApi([(401, None)])

    result = _ask(db_session, api, monkeypatch)

    assert result is False
    db_session.refresh(rejected)
    assert rejected.status == openai_key_service.STATUS_INVALID


def test_scope_decision_fails_open_when_every_key_fails(db_session, monkeypatch) -> None:
    openai_key_service.create_key(
        db_session, name="First", api_key="sk-first-0004", priority=1
    )
    openai_key_service.create_key(
        db_session, name="Second", api_key="sk-second-0005", priority=2
    )
    api = _FakeApi([(500, None), (503, None)])

    result = _ask(db_session, api, monkeypatch)

    assert result is False
    assert api.auth_headers == ["Bearer sk-first-0004", "Bearer sk-second-0005"]


def test_scope_decision_skips_the_call_without_a_key(db_session, monkeypatch) -> None:
    class ExplodingClient:
        def __init__(self, **_: object) -> None:
            raise AssertionError("the scope gate must not call OpenAI without a key")

    monkeypatch.setattr(openai_decision_service.httpx, "AsyncClient", ExplodingClient)

    result = asyncio.run(
        openai_decision_service.is_confidently_out_of_scope(
            class_name="C++ Basic",
            class_description=None,
            question="How do Python lists work?",
            history="",
            db=db_session,
        )
    )

    assert result is False
