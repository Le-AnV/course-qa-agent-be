"""Rotation behaviour of the Gemini gateway (first-token budget, sticky key)."""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import gemini_key_service
from app.services.llm import gateway
from app.services.llm.exceptions import LlmConnectionError

MODEL = "models/gemini-3.5-flash-lite"


class _FakeStream:
    def __init__(self, *, delay: float, chunks: list[str]) -> None:
        self._delay = delay
        self._chunks = chunks

    def astream(self, _messages):
        async def gen():
            await asyncio.sleep(self._delay)
            for chunk in self._chunks:
                yield SimpleNamespace(content=chunk)

        return gen()


def _resolved(name: str) -> gemini_key_service.ResolvedGeminiKey:
    return gemini_key_service.ResolvedGeminiKey(
        api_key=name,
        key_id=uuid.uuid4(),
        key_name=name,
        key=None,
    )


def _key_resolver(resolved_keys, captured: list | None = None):
    def resolve(db, *, model, exclude_ids=None, preferred_id=None):
        if captured is not None:
            captured.append(preferred_id)
        for resolved in resolved_keys:
            if resolved.key_id not in (exclude_ids or set()):
                return resolved
        return None

    return resolve


async def _collect(**kwargs) -> list[str]:
    return [chunk async for chunk in gateway.astream_chat(MagicMock(), [], **kwargs)]


def test_stream_rotates_after_a_first_token_timeout(monkeypatch) -> None:
    first, second = _resolved("slow"), _resolved("fast")
    monkeypatch.setattr(
        gateway,
        "gemini_key_service",
        SimpleNamespace(
            resolve_key=_key_resolver([first, second]),
            model_id=gemini_key_service.model_id,
            FALLBACK_CHAT_MODEL=gemini_key_service.FALLBACK_CHAT_MODEL,
            mark_key_failure=lambda *args, **kwargs: None,
            touch_key=lambda *args, **kwargs: None,
            get_auto_switch=lambda *args, **kwargs: True,
        ),
    )
    monkeypatch.setattr(
        gateway,
        "get_llm_model",
        lambda *, model, api_key: _FakeStream(
            delay=0.2 if api_key == "slow" else 0.0,
            chunks=["hi"],
        ),
    )

    chunks = asyncio.run(
        _collect(model=MODEL, first_chunk_timeout=0.05, auto_switch=True)
    )

    assert chunks == ["hi"]


def test_stream_stops_at_the_first_token_budget(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "FIRST_TOKEN_BUDGET_SECONDS", 0.15)
    keys = [_resolved(f"k{index}") for index in range(3)]
    monkeypatch.setattr(
        gateway,
        "gemini_key_service",
        SimpleNamespace(
            resolve_key=_key_resolver(keys),
            model_id=gemini_key_service.model_id,
            FALLBACK_CHAT_MODEL=gemini_key_service.FALLBACK_CHAT_MODEL,
            mark_key_failure=lambda *args, **kwargs: None,
            touch_key=lambda *args, **kwargs: None,
            get_auto_switch=lambda *args, **kwargs: True,
        ),
    )
    monkeypatch.setattr(
        gateway,
        "get_llm_model",
        lambda *, model, api_key: _FakeStream(delay=5.0, chunks=["never"]),
    )

    started = time.monotonic()
    with pytest.raises(LlmConnectionError):
        asyncio.run(_collect(model=MODEL, first_chunk_timeout=5.0, auto_switch=True))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0


def test_successful_key_is_tried_first_next_time(monkeypatch) -> None:
    gateway._last_good_keys.clear()
    good = _resolved("good")
    captured: list = []
    monkeypatch.setattr(
        gateway,
        "gemini_key_service",
        SimpleNamespace(
            resolve_key=_key_resolver([good], captured),
            model_id=gemini_key_service.model_id,
            FALLBACK_CHAT_MODEL=gemini_key_service.FALLBACK_CHAT_MODEL,
            mark_key_failure=lambda *args, **kwargs: None,
            touch_key=lambda *args, **kwargs: None,
            get_auto_switch=lambda *args, **kwargs: True,
        ),
    )
    monkeypatch.setattr(
        gateway,
        "get_llm_model",
        lambda *, model, api_key: _FakeStream(delay=0.0, chunks=["ok"]),
    )

    asyncio.run(_collect(model=MODEL, auto_switch=True))
    asyncio.run(_collect(model=MODEL, auto_switch=True))

    assert captured[0] is None
    assert captured[-1] == good.key_id
    gateway._last_good_keys.clear()


def test_side_calls_avoid_the_key_that_is_streaming(monkeypatch) -> None:
    gateway._busy_keys.clear()
    gateway._last_good_keys.clear()
    streaming, idle = _resolved("streaming"), _resolved("idle")
    gateway._busy_keys.add(streaming.key_id)
    chosen: list[str] = []

    def resolve(db, *, model, exclude_ids=None, preferred_id=None):
        for resolved in (streaming, idle):
            if resolved.key_id not in (exclude_ids or set()):
                chosen.append(resolved.key_name)
                return resolved
        return None

    monkeypatch.setattr(
        gateway,
        "gemini_key_service",
        SimpleNamespace(
            resolve_key=resolve,
            model_id=gemini_key_service.model_id,
            FALLBACK_CHAT_MODEL=gemini_key_service.FALLBACK_CHAT_MODEL,
            mark_key_failure=lambda *args, **kwargs: None,
            touch_key=lambda *args, **kwargs: None,
            get_auto_switch=lambda *args, **kwargs: True,
        ),
    )

    class FakeModel:
        async def ainvoke(self, _messages):
            return SimpleNamespace(content="ok")

    monkeypatch.setattr(
        gateway, "get_llm_model", lambda *, model, api_key: FakeModel()
    )

    answer = asyncio.run(gateway.ainvoke_chat(MagicMock(), [], model=MODEL))

    assert answer == "ok"
    assert chosen[0] == "idle"
    gateway._busy_keys.clear()
