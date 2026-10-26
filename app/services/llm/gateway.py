"""Failover-aware Gemini calls shared by chat, RAG, and follow-up resolution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.services import gemini_key_service
from app.services.llm.exceptions import LlmConnectionError
from app.services.llm.provider import get_llm_model

logger = logging.getLogger(__name__)

FIRST_CHUNK_TIMEOUT_SECONDS = 12.0
DEFAULT_INVOKE_TIMEOUT_SECONDS = 60.0


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

    for current_model in _candidate_models(primary_model, allow_fallback):
        excluded: set[Any] = set()
        while True:
            resolved = gemini_key_service.resolve_key(
                db,
                model=current_model,
                exclude_ids=excluded,
            )
            if resolved is None:
                break

            yielded = False
            try:
                llm = get_llm_model(model=current_model, api_key=resolved.api_key)
                stream = llm.astream(messages).__aiter__()
                first = await asyncio.wait_for(stream.__anext__(), timeout=first_chunk_timeout)
                text = _text(getattr(first, "content", first))
                if text:
                    yielded = True
                    yield text
                async for chunk in stream:
                    text = _text(getattr(chunk, "content", chunk))
                    if text:
                        yielded = True
                        yield text
                _record_success(db, resolved)
                return
            except StopAsyncIteration:
                _record_success(db, resolved)
                return
            except Exception as error:
                last_error = error
                _mark_failure(db, resolved, error)
                if not _auto_switch_enabled(db, auto_switch):
                    raise
                if yielded:
                    yield StreamRestart()
                if resolved.key_id is None:
                    break
                excluded.add(resolved.key_id)
                logger.warning(
                    "Gemini key %s failed for %s; trying the next key.",
                    resolved.key_name,
                    current_model,
                )

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
                )
                if resolved is None:
                    break
                try:
                    llm = get_llm_model(model=current_model, api_key=resolved.api_key)
                    response = llm.invoke(messages)
                    answer = _text(getattr(response, "content", response))
                    _record_success(session, resolved)
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
        for current_model in _candidate_models(primary_model, allow_fallback):
            excluded: set[Any] = set()
            while True:
                resolved = gemini_key_service.resolve_key(
                    session,
                    model=current_model,
                    exclude_ids=excluded,
                )
                if resolved is None:
                    break
                try:
                    llm = get_llm_model(model=current_model, api_key=resolved.api_key)
                    response = await asyncio.wait_for(llm.ainvoke(messages), timeout=timeout)
                    answer = _text(getattr(response, "content", response))
                    _record_success(session, resolved)
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
