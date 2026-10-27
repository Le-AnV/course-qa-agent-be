"""Class scope decision used before class document retrieval.

The gate compares the student's question with the class syllabus (name and
description). It is provider-agnostic: Gemini is the default because the
application already holds a Gemini key, and any OpenAI-compatible
``/chat/completions`` endpoint can be selected instead. Only the transport
changes; the prompts, confidence threshold and fail-open contract are shared.

The model answers with a verdict plus a confidence, and only a confident
``out_of_scope`` blocks the turn. Missing credentials, provider failures, and
malformed payloads all fail open so the existing RAG flow stays available.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal

import httpx
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config.config import settings
from app.core.prompt_manager import get_prompt_manager
from app.services import gemini_key_service, openai_key_service

logger = logging.getLogger(__name__)

IN_SCOPE = "in_scope"
OUT_OF_SCOPE = "out_of_scope"

# A transient provider failure must not make every later turn pay the same
# timeout. The primary model is skipped until this monotonic timestamp.
_primary_cooldown_until = 0.0


def reset_failure_state() -> None:
    """Clear the primary-model cooldown (tests and process restarts)."""

    global _primary_cooldown_until
    _primary_cooldown_until = 0.0


def _cooldown_remaining() -> float:
    """Seconds left before the primary model may be tried again."""

    return max(0.0, _primary_cooldown_until - time.monotonic())


def _start_cooldown() -> None:
    """Skip the primary model for ``DECISION_COOLDOWN_SECONDS``."""

    global _primary_cooldown_until
    _primary_cooldown_until = time.monotonic() + settings.decision_cooldown_seconds


def _fallback_model() -> str:
    """Return the configured fallback model, or ``""`` when it is unusable."""

    if not settings.decision_fallback_enabled:
        return ""
    model = settings.decision_fallback_model.strip()
    return "" if model == settings.decision_model else model


def _primary_timeout(total_budget: float, fallback_model: str) -> float:
    """Reserve part of the decision budget for the fallback model."""

    if not fallback_model:
        return total_budget
    reserve = min(settings.decision_fallback_timeout_seconds, total_budget / 2)
    return max(1.0, total_budget - reserve)


class ScopeDecision(BaseModel):
    """One scope verdict returned by the decision model."""

    verdict: Literal["in_scope", "out_of_scope"]
    confidence: float


# Structured output keeps the verdict machine-readable instead of parsing prose.
# Both the Gemini schema and the OpenAI response_format use the same fields.
_OPENAI_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "scope_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": [IN_SCOPE, OUT_OF_SCOPE]},
                "confidence": {"type": "number"},
            },
            "required": ["verdict", "confidence"],
            "additionalProperties": False,
        },
    },
}


def _is_confident_out_of_scope(decision: ScopeDecision | None) -> bool:
    """Block only a negative verdict that clears the configured confidence."""

    return (
        decision is not None
        and decision.verdict == OUT_OF_SCOPE
        and decision.confidence >= settings.decision_confidence_threshold
    )


def _parse_decision(content: str) -> ScopeDecision:
    """Validate a raw JSON payload against the shared decision schema."""

    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Scope decision is not valid JSON: {content!r}") from exc
    return ScopeDecision.model_validate(payload)


async def _decide_with_gemini(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,
    timeout: float,
    api_key: str,
) -> ScopeDecision:
    """Ask Gemini for a structured verdict through the native SDK."""

    client = genai.Client(
        api_key=api_key,
        http_options=genai_types.HttpOptions(
            # The Gemini SDK rejects a manual deadline below 10s. The real
            # per-attempt budget is enforced by asyncio.wait_for below.
            timeout=max(10_000, int(timeout * 1000)),
            # One attempt: the gate has its own bounded wait and must fail open
            # quickly instead of multiplying the latency through hidden retries.
            retryOptions=genai_types.HttpRetryOptions(attempts=1),
        ),
    )
    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=ScopeDecision,
                    temperature=0.0,
                    max_output_tokens=settings.decision_max_output_tokens,
                ),
            ),
            timeout=timeout,
        )
    finally:
        await client.aio.aclose()

    parsed = response.parsed
    if isinstance(parsed, ScopeDecision):
        return parsed
    if isinstance(parsed, dict):
        return ScopeDecision.model_validate(parsed)
    return _parse_decision(response.text or "")


async def _decide_with_openai(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,
    timeout: float,
    api_key: str,
) -> ScopeDecision:
    """Ask any OpenAI-compatible /chat/completions endpoint for a verdict."""

    client_timeout = httpx.Timeout(
        timeout,
        connect=min(settings.decision_connect_timeout_seconds, timeout),
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": _OPENAI_RESPONSE_FORMAT,
        "max_completion_tokens": settings.decision_max_output_tokens,
    }
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        response = await client.post(
            f"{settings.decision_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]

    return _parse_decision(content)


def _env_key(provider: str) -> str | None:
    """Read the environment key used when no admin-managed key is healthy."""

    secret = settings.google_api_key if provider == "gemini" else settings.openai_api_key
    if secret is None:
        return None
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
    return value or None


def _resolve_key(
    provider: str,
    model: str,
    db: Session | None,
    excluded: set[Any],
) -> tuple[str | None, Any]:
    """Return an API key plus the admin key record it came from.

    Admin-managed pools are preferred so a rejected or rate-limited key is
    rotated out. Without a database (unit tests, scripts) the environment key is
    used directly.
    """

    if db is None:
        return _env_key(provider), None

    if provider == "gemini":
        resolved = gemini_key_service.resolve_key(db, model=model, exclude_ids=excluded)
    else:
        resolved = openai_key_service.resolve_key(db, exclude_ids=excluded)
    if resolved is None:
        return None, None
    return resolved.api_key, resolved


def _record_success(provider: str, db: Session | None, record: Any) -> None:
    """Mark a pool key as used after a successful verdict."""

    key = getattr(record, "key", None)
    if db is None or key is None:
        return
    service = gemini_key_service if provider == "gemini" else openai_key_service
    try:
        service.touch_key(db, key)
    except Exception:
        logger.exception("Failed to record scope decision key use.")


def _record_failure(provider: str, db: Session | None, record: Any, error: BaseException) -> None:
    """Disable or cool down a pool key after a provider error."""

    key = getattr(record, "key", None)
    if db is None or key is None:
        return
    service = gemini_key_service if provider == "gemini" else openai_key_service
    try:
        service.mark_key_failure(db, key, error)
    except Exception:
        logger.exception("Failed to record scope decision key failure.")


async def _call_provider(
    provider: str,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
    db: Session | None = None,
) -> ScopeDecision:
    """Dispatch one decision attempt, rotating keys when a pool is available."""

    attempts = max(1, settings.decision_key_attempts)
    deadline = time.monotonic() + timeout
    excluded: set[Any] = set()
    last_error: Exception | None = None

    for _ in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Scope decision budget was exhausted before a key answered.")

        api_key, record = _resolve_key(provider, model, db, excluded)
        if api_key is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError(f"No {provider} key is available for the scope decision.")

        try:
            if provider == "gemini":
                decision = await _decide_with_gemini(
                    system_prompt,
                    user_prompt,
                    model=model,
                    timeout=remaining,
                    api_key=api_key,
                )
            else:
                decision = await _decide_with_openai(
                    system_prompt,
                    user_prompt,
                    model=model,
                    timeout=remaining,
                    api_key=api_key,
                )
        except Exception as exc:
            last_error = exc
            _record_failure(provider, db, record, exc)
            key_id = getattr(record, "key_id", None)
            if key_id is None:
                raise
            logger.warning(
                "Scope decision key %s failed (%s); trying the next key.",
                getattr(record, "key_name", "?"),
                type(exc).__name__,
            )
            excluded.add(key_id)
            continue

        _record_success(provider, db, record)
        return decision

    raise last_error or RuntimeError("No usable scope decision key.")


async def _decide_with_fallback(
    provider: str,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
    db: Session | None = None,
) -> ScopeDecision | None:
    """Try the fallback model once; any failure still fails open."""

    started = time.monotonic()
    try:
        decision = await _call_provider(
            provider,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=timeout,
            db=db,
        )
    except Exception as exc:
        logger.warning(
            "Scope decision fallback %s failed after %.2fs (%s); continuing as in scope.",
            model,
            time.monotonic() - started,
            exc,
        )
        logger.debug("Scope fallback failure details.", exc_info=True)
        return None

    logger.info(
        "Scope decision %s (%.2f) via %s/%s fallback in %.2fs.",
        decision.verdict,
        decision.confidence,
        provider,
        model,
        time.monotonic() - started,
    )
    return decision


async def _decide(
    prompt_name: str,
    *,
    db: Session | None = None,
    **prompt_vars: str,
) -> ScopeDecision | None:
    """Ask the configured decision provider, or ``None`` when unavailable.

    ``DECISION_TIMEOUT_SECONDS`` is the budget for the whole decision. When a
    fallback model is configured, part of that budget is reserved for it, so a
    primary timeout does not consume the whole gate. A failing primary model is
    skipped for ``DECISION_COOLDOWN_SECONDS`` and later turns go straight to the
    fallback instead of paying the same timeout again.
    """

    provider = settings.decision_provider
    try:
        prompt = get_prompt_manager().format(prompt_name, **prompt_vars)
        system_prompt = str(prompt["system-prompt"])
        user_prompt = str(prompt["user-prompt"])
    except Exception as exc:
        logger.warning(
            "Scope decision prompt %s could not be built; continuing as in scope: %s",
            prompt_name,
            exc,
        )
        logger.debug("Scope prompt failure details.", exc_info=True)
        return None

    total_budget = settings.decision_timeout_seconds
    fallback_model = _fallback_model()

    cooldown_left = _cooldown_remaining()
    if fallback_model and cooldown_left > 0:
        logger.info(
            "Scope decision primary %s skipped for another %.1fs (cooldown); using fallback %s.",
            settings.decision_model,
            cooldown_left,
            fallback_model,
        )
        return await _decide_with_fallback(
            provider,
            model=fallback_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=total_budget,
            db=db,
        )

    primary_timeout = _primary_timeout(total_budget, fallback_model)
    started = time.monotonic()
    try:
        decision = await _call_provider(
            provider,
            model=settings.decision_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=primary_timeout,
            db=db,
        )
    except Exception as exc:
        logger.warning(
            "Scope decision primary %s failed after %.2fs (%s).",
            settings.decision_model,
            time.monotonic() - started,
            exc,
        )
        logger.debug("Scope primary failure details.", exc_info=True)
        _start_cooldown()
        if not fallback_model:
            logger.warning(
                "Scope decision unavailable; skipping the primary model for %.0fs and "
                "continuing as in scope.",
                settings.decision_cooldown_seconds,
            )
            return None

        logger.info(
            "Trying scope decision fallback %s for up to %.1fs; primary %s skipped for %.0fs.",
            fallback_model,
            min(settings.decision_fallback_timeout_seconds, total_budget),
            settings.decision_model,
            settings.decision_cooldown_seconds,
        )
        return await _decide_with_fallback(
            provider,
            model=fallback_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=min(settings.decision_fallback_timeout_seconds, total_budget),
            db=db,
        )

    reset_failure_state()
    logger.info(
        "Scope decision %s (%.2f) via %s/%s in %.2fs.",
        decision.verdict,
        decision.confidence,
        provider,
        settings.decision_model,
        time.monotonic() - started,
    )
    return decision


async def is_confidently_out_of_scope(
    *,
    class_name: str,
    class_description: str | None,
    question: str,
    history: str,
    db: Session | None = None,
) -> bool:
    """Return true only for a confident out-of-scope decision for this class.

    ``db`` lets the gate use the admin-managed key pools; without it the
    environment keys are used directly.
    """

    decision = await _decide(
        "class-scope-prompt",
        db=db,
        class_name=class_name,
        class_description=class_description or "",
        question=question,
        history=history,
    )
    return _is_confident_out_of_scope(decision)
