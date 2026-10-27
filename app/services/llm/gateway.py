"""Failover-aware Gemini calls shared by chat, RAG, and follow-up resolution."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.services import gemini_key_service
from app.services.llm.exceptions import LlmConnectionError
from app.services.llm.provider import get_llm_model

logger = logging.getLogger(__name__)

PRIMARY_FIRST_CHUNK_TIMEOUT_SECONDS = 8.0
RETRY_FIRST_CHUNK_TIMEOUT_SECONDS = 8.0
FIRST_CHUNK_TIMEOUT_SECONDS = PRIMARY_FIRST_CHUNK_TIMEOUT_SECONDS
FIRST_TOKEN_BUDGET_SECONDS = 20.0
DEFAULT_INVOKE_TIMEOUT_SECONDS = 60.0

# Model id -> key id that last produced an answer. Sticky routing avoids
# rotating away from a key that is known to work.
_last_good_keys: dict[str, Any] = {}

# Keys currently streaming an answer. Side calls (follow-up questions) skip
# them so one free-tier key never serves two generations at once.
_busy_keys: set[Any] = set()


def _select_key(
    db: Session,
    model: str,
    excluded: set[Any],
    preferred_id: Any = None,
    *,
    avoid_sticky: bool = False,
) -> gemini_key_service.ResolvedGeminiKey | None:
    """Resolve a key, preferring one that is not already streaming."""

    preferred = preferred_id or _last_good_keys.get(
        gemini_key_service.model_id(model)
    )
    if avoid_sticky:
        skip = excluded | _busy_keys | {preferred}
    else:
        skip = excluded | _busy_keys
    resolved = gemini_key_service.resolve_key(
        db,
        model=model,
        exclude_ids=skip,
        preferred_id=None if avoid_sticky else preferred,
    )
    if resolved is None:
        resolved = gemini_key_service.resolve_key(
            db,
            model=model,
            exclude_ids=excluded,
            preferred_id=preferred,
        )
    return resolved


@dataclass(frozen=True)
class StreamRestart:
    """Ask the caller to clear partial text before the retry starts."""

    message: str = "Switching to the next Gemini key."


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _record_success(
    db: Session,
    resolved: gemini_key_service.ResolvedGeminiKey,
) -> None:
    if resolved.key is None:
        return
    try:
        gemini_key_service.touch_key(db, resolved.key)
    except Exception:
        logger.exception("Failed to mark Gemini key %s as used.", resolved.key_id)


def _mark_failure(
    db: Session,
    resolved: gemini_key_service.ResolvedGeminiKey,
    error: BaseException,
) -> None:
    if resolved.key is None:
        return
    try:
        gemini_key_service.mark_key_failure(db, resolved.key, error)
    except Exception:
        logger.exception("Failed to record Gemini key failure for %s.", resolved.key_id)


def _auto_switch_enabled(db: Session, auto_switch: bool | None) -> bool:
    return (
        auto_switch
        if auto_switch is not None
        else gemini_key_service.get_auto_switch(db)
    )


def _candidate_models(model: str, allow_fallback: bool) -> list[str]:
    models = [model]
    if allow_fallback and gemini_key_service.model_id(model) != gemini_key_service.model_id(
        gemini_key_service.FALLBACK_CHAT_MODEL
    ):
        models.append(gemini_key_service.FALLBACK_CHAT_MODEL)
    return models


async def _aclose_stream(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        logger.debug("Failed to close a Gemini stream cleanly.", exc_info=True)


async def _open_stream(
    resolved: gemini_key_service.ResolvedGeminiKey,
    model: str,
    messages: Sequence[Any],
    timeout: float,
) -> tuple[Any, str]:
    """Start a stream and wait for its first chunk (or fail fast)."""

    llm = get_llm_model(model=model, api_key=resolved.api_key)
    stream = llm.astream(messages).__aiter__()
    try:
        first = await asyncio.wait_for(stream.__anext__(), timeout=timeout)
    except BaseException:
        await _aclose_stream(stream)
        raise
    return stream, _text(getattr(first, "content", first))


async def astream_chat(
    db: Session,
    messages: Sequence[Any],
    *,
    model: str | None = None,
    allow_fallback: bool = True,
    first_chunk_timeout: float = FIRST_CHUNK_TIMEOUT_SECONDS,
    auto_switch: bool | None = None,
) -> AsyncIterator[str | StreamRestart]:
    """Stream a chat answer with key rotation.

    A failure before the first token retries on the next key without the caller
    noticing. A failure after text was emitted yields ``StreamRestart`` so the
    caller can clear the partial answer and retry on the next key.
    """

    primary_model = model or gemini_key_service.get_chat_model(db)
    last_error: BaseException | None = None
    deadline = time.monotonic() + FIRST_TOKEN_BUDGET_SECONDS
    attempt = 0

    for current_model in _candidate_models(primary_model, allow_fallback):
        excluded: set[Any] = set()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            key_switch = _auto_switch_enabled(db, auto_switch)
            attempt_timeout = min(
                first_chunk_timeout if attempt == 0 else RETRY_FIRST_CHUNK_TIMEOUT_SECONDS,
                remaining,
            )
            primary = _select_key(db, current_model, excluded)
            if primary is None:
                break

            tasks: dict[Any, Any] = {}
            primary_task = asyncio.create_task(
                _open_stream(primary, current_model, messages, attempt_timeout)
            )
            tasks[primary_task] = primary
            if key_switch:
                secondary = _select_key(
                    db, current_model, excluded | {primary.key_id}
                )
                if secondary is not None:
                    secondary_task = asyncio.create_task(
                        _open_stream(
                            secondary, current_model, messages, attempt_timeout
                        )
                    )
                    tasks[secondary_task] = secondary

            done, pending = await asyncio.wait(
                set(tasks),
                timeout=attempt_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            winner: tuple[Any, Any] | None = None
            for task in done:
                resolved = tasks[task]
                error = task.exception()
                if isinstance(error, StopAsyncIteration):
                    winner = winner or (task, resolved)
                    continue
                if error is None:
                    winner = winner or (task, resolved)
                    continue
                last_error = error
                _mark_failure(db, resolved, error)
                if not key_switch:
                    raise error
                if resolved.key_id is not None:
                    excluded.add(resolved.key_id)
                attempt += 1
                logger.warning(
                    "Gemini key %s failed for %s; trying the next key.",
                    resolved.key_name,
                    current_model,
                )

            if winner is None:
                if pending:
                    timeout_error = TimeoutError("Gemini first token timed out.")
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in pending:
                        resolved = tasks[task]
                        last_error = timeout_error
                        _mark_failure(db, resolved, timeout_error)
                        if resolved.key_id is not None:
                            excluded.add(resolved.key_id)
                        attempt += 1
                continue

            winning_task, resolved = winner
            for task in tasks:
                if task is winning_task:
                    continue
                task.cancel()
            await asyncio.gather(
                *[task for task in tasks if task is not winning_task],
                return_exceptions=True,
            )
            try:
                opened = winning_task.result()
            except StopAsyncIteration:
                opened = None
            if opened is None:
                return

            stream, first_text = opened
            primary = resolved
            _record_success(db, primary)
            if primary.key_id is not None:
                _last_good_keys[gemini_key_service.model_id(current_model)] = (
                    primary.key_id
                )
                _busy_keys.add(primary.key_id)

            restart = False
            try:
                if first_text:
                    yield first_text
                async for chunk in stream:
                    text = _text(getattr(chunk, "content", chunk))
                    if text:
                        yield text
            except Exception as error:
                last_error = error
                _mark_failure(db, primary, error)
                if not key_switch:
                    raise
                if primary.key_id is not None:
                    excluded.add(primary.key_id)
                attempt += 1
                yield StreamRestart()
                restart = True
            finally:
                await _aclose_stream(stream)
                if primary.key_id is not None:
                    _busy_keys.discard(primary.key_id)

            if not restart:
                return

    raise LlmConnectionError("Gemini is temporarily unavailable. Please try again.") from last_error


def invoke_chat(
    db: Session | None,
    messages: Sequence[Any],
    *,
    model: str | None = None,
    allow_fallback: bool = True,
    auto_switch: bool | None = None,
) -> str:
    """Synchronous non-streaming call for legacy RAG callers."""

    if db is None:
        from app.config import settings
        from app.core.database import get_session_local

        session = get_session_local(settings.database_url)()
    else:
        session = db

    owns_session = db is None
    try:
        primary_model = model or gemini_key_service.get_chat_model(session)
        last_error: BaseException | None = None
        for current_model in _candidate_models(primary_model, allow_fallback):
            excluded: set[Any] = set()
            while True:
                resolved = gemini_key_service.resolve_key(
                    session,
                    model=current_model,
                    exclude_ids=excluded,
                    preferred_id=_last_good_keys.get(
                        gemini_key_service.model_id(current_model)
                    ),
                )
                if resolved is None:
                    break
                try:
                    llm = get_llm_model(model=current_model, api_key=resolved.api_key)
                    response = llm.invoke(messages)
                    answer = _text(getattr(response, "content", response))
                    _record_success(session, resolved)
                    if resolved.key_id is not None:
                        _last_good_keys[gemini_key_service.model_id(current_model)] = (
                            resolved.key_id
                        )
                    return answer
                except Exception as error:
                    last_error = error
                    _mark_failure(session, resolved, error)
                    if not _auto_switch_enabled(session, auto_switch):
                        raise
                    if resolved.key_id is None:
                        break
                    excluded.add(resolved.key_id)
        raise LlmConnectionError(
            "Gemini is temporarily unavailable. Please try again."
        ) from last_error
    finally:
        if owns_session:
            session.close()


async def ainvoke_chat(
    db: Session | None,
    messages: Sequence[Any],
    *,
    model: str | None = None,
    allow_fallback: bool = True,
    timeout: float = DEFAULT_INVOKE_TIMEOUT_SECONDS,
    auto_switch: bool | None = None,
    avoid_sticky_key: bool = False,
) -> str:
    """Non-streaming call for RAG/condensation with the same key rotation."""

    if db is None:
        from app.config import settings
        from app.core.database import get_session_local

        session = get_session_local(settings.database_url)()
    else:
        session = db

    owns_session = db is None
    try:
        primary_model = model or gemini_key_service.get_chat_model(session)
        last_error: BaseException | None = None
        deadline = time.monotonic() + timeout
        for current_model in _candidate_models(primary_model, allow_fallback):
            excluded: set[Any] = set()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                resolved = _select_key(
                    session,
                    current_model,
                    excluded,
                    avoid_sticky=avoid_sticky_key,
                )
                if resolved is None:
                    break
                try:
                    llm = get_llm_model(model=current_model, api_key=resolved.api_key)
                    response = await asyncio.wait_for(
                        llm.ainvoke(messages), timeout=min(remaining, 30.0)
                    )
                    answer = _text(getattr(response, "content", response))
                    _record_success(session, resolved)
                    if resolved.key_id is not None:
                        _last_good_keys[gemini_key_service.model_id(current_model)] = (
                            resolved.key_id
                        )
                    return answer
                except Exception as error:
                    last_error = error
                    _mark_failure(session, resolved, error)
                    if not _auto_switch_enabled(session, auto_switch):
                        raise
                    if resolved.key_id is None:
                        break
                    excluded.add(resolved.key_id)
        raise LlmConnectionError(
            "Gemini is temporarily unavailable. Please try again."
        ) from last_error
    finally:
        if owns_session:
            session.close()
