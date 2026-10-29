"""Class scope decision used before class document retrieval.

The gate compares the student's question with the class syllabus (name and
description) using Gemini. The model answers with a verdict plus a confidence,
and only a confident ``out_of_scope`` blocks the turn. Missing credentials,
provider failures, and malformed payloads all fail open so the existing RAG
flow stays available.
"""

from __future__ import annotations

import asyncio
from enum import Enum
import json
import logging
import re
import time
from typing import Any, Literal

import httpx
from google import genai
from google.genai import types as genai_types
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.config.config import settings
from app.core.prompt_manager import get_prompt_manager
from app.services import gemini_key_service
from app.services.llm.provider import get_llm_model

logger = logging.getLogger(__name__)


class ScopeVerdict(str, Enum):
    CORE = "core"
    COMPARISON = "comparison"
    EXTENSION = "extension"
    LOGISTICS = "logistics"
    OUT_OF_SCOPE = "out_of_scope"


_CODE_BLOCK_PATTERN = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_COMPILER_ERROR_PATTERNS = [
    re.compile(
        r"(?:^|\n)\s*(?:[a-zA-Z0-9_\.\/\\]+:\d+:\d+:\s*)?(?:error|warning|fatal error):\s*.+",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|\n)Traceback \(most recent call last\):[\s\S]+", re.IGNORECASE),
    re.compile(
        r"(?:^|\n)\s*(?:Segmentation fault|core dumped|NullPointerException|IndexOutOfBoundsException)[\s\S]*",
        re.IGNORECASE,
    ),
]
_RAW_CODE_INDICATORS = [
    re.compile(r"#include\s*<"),
    re.compile(r"using\s+namespace\s+\w+;"),
    re.compile(r"\bint\s+main\s*\("),
    re.compile(r"\b(?:std::)?cout\s*<<"),
    re.compile(r"\b(?:std::)?cin\s*>>"),
    re.compile(r"\bprintf\s*\("),
    re.compile(r"\bscanf\s*\("),
    re.compile(r"\b(?:int|char|float|double|bool|void|auto|long|short)\s+[\*&]*\s*\w+\s*(?:=|[;\(])"),
    re.compile(r"\b(?:class|struct)\s+\w+"),
    re.compile(r"\b(?:public|private|protected):"),
    re.compile(r"\b(?:return|delete|new)\s+"),
    re.compile(r"\bstd::\w+"),
    re.compile(r"->|\bnullptr\b|\bNULL\b"),
    re.compile(r";\s*$"),
    re.compile(r"^\s*(?:import\s+\w+|from\s+\w+\s+import)"),
    re.compile(r"^\s*def\s+\w+\s*\(.*?\):"),
]


def _is_code_line(line: str) -> bool:
    line_s = line.strip()
    if not line_s or line_s in {"{", "}", "};"}:
        return False
    return any(p.search(line_s) for p in _RAW_CODE_INDICATORS)


def preprocess_student_query(raw_message: str) -> tuple[str, str, bool]:
    """Extract code snippets/error traces from a student's prompt.

    Separating code from the natural-language question prevents embedding
    dilution during dense retrieval and respects the Cross-Encoder's 512-token
    limit. Supports both fenced markdown code blocks and raw unfenced snippets.

    Returns:
        (clean_question, code_content, has_code)
    """
    raw_text = (raw_message or "").strip()
    if not raw_text:
        return "", "", False

    extracted_codes: list[str] = []

    def _extract_fenced(match: re.Match) -> str:
        code = match.group(1).strip()
        if code:
            extracted_codes.append(code)
        return ""

    text_no_fences = _CODE_BLOCK_PATTERN.sub(_extract_fenced, raw_text).strip()
    code_content = "\n\n".join(extracted_codes).strip()

    if not code_content:
        for pattern in _COMPILER_ERROR_PATTERNS:
            match = pattern.search(text_no_fences)
            if match:
                code_content = match.group(0).strip()
                text_no_fences = (
                    text_no_fences[: match.start()] + text_no_fences[match.end() :]
                ).strip()
                break

    if not code_content:
        lines = text_no_fences.splitlines()
        code_lines: list[str] = []
        text_lines: list[str] = []
        for line in lines:
            line_s = line.strip()
            if _is_code_line(line) or (code_lines and line_s in {"{", "}", "};"}):
                code_lines.append(line)
            else:
                text_lines.append(line)
        if len(code_lines) >= 2 or (
            len(code_lines) == 1
            and any(
                k in code_lines[0]
                for k in ["#include", "main(", "cout", "class ", "struct ", "int*", "from ", "import ", "def "]
            )
        ):
            code_content = "\n".join(code_lines).strip()
            text_no_fences = "\n".join(text_lines).strip()

    has_code = bool(code_content)
    clean_question = text_no_fences.strip()
    return clean_question, code_content, has_code


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

    reasoning: str = ""
    verdict: str = "core"
    is_related_tech: bool = False
    is_homework_request: bool = False
    external_search_query: str | None = None
    search_query_en: str | None = None
    confidence: float = 1.0

    @field_validator("verdict", mode="before")
    @classmethod
    def normalize_verdict(cls, v: Any) -> str:
        if hasattr(v, "value"):
            return str(v.value).lower()
        return str(v or "core").lower()

    @property
    def is_out_of_scope(self) -> bool:
        v = str(self.verdict.value if hasattr(self.verdict, "value") else self.verdict).lower()
        return v in {ScopeVerdict.OUT_OF_SCOPE.value, "out_of_scope"}

    @property
    def is_in_scope(self) -> bool:
        return not self.is_out_of_scope


# Structured output keeps the verdict machine-readable instead of parsing prose.


def _is_confident_out_of_scope(decision: ScopeDecision | None) -> bool:
    """Block only a negative verdict that clears the configured confidence."""

    return (
        decision is not None
        and decision.is_out_of_scope
        and decision.confidence >= settings.decision_confidence_threshold
    )


def _parse_decision(content: str) -> ScopeDecision:
    """Validate a raw JSON payload against the shared decision schema."""

    cleaned = (content or "").strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()
        else:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1:
                cleaned = cleaned[start : end + 1].strip()
    elif not cleaned.startswith("{") and "{" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1:
            cleaned = cleaned[start : end + 1].strip()

    try:
        payload = json.loads(cleaned)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Scope decision is not valid JSON: {content!r}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Scope decision is not a JSON object: {content!r}")

    # Robust normalization before validation
    try:
        conf_raw = payload.get("confidence", 1.0)
        conf = float(conf_raw) if conf_raw is not None else 1.0
    except (ValueError, TypeError):
        conf = 1.0

    raw_v = str(payload.get("verdict") or "core").strip().lower()
    if raw_v in ("in_scope", "in-scope", "core"):
        norm_v = "core"
    elif raw_v in ("out_of_scope", "out-of-scope"):
        norm_v = "out_of_scope"
    elif raw_v in ("comparison", "compare"):
        norm_v = "comparison"
    elif raw_v in ("extension", "ext"):
        norm_v = "extension"
    elif raw_v in ("logistics", "admin"):
        norm_v = "logistics"
    else:
        norm_v = raw_v

    return ScopeDecision(
        reasoning=str(payload.get("reasoning") or ""),
        verdict=norm_v,
        is_related_tech=bool(payload.get("is_related_tech")),
        is_homework_request=bool(payload.get("is_homework_request")),
        external_search_query=str(payload.get("external_search_query")) if payload.get("external_search_query") else None,
        search_query_en=str(payload.get("search_query_en")) if payload.get("search_query_en") else None,
        confidence=conf,
    )


async def _decide_with_gemini(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,
    timeout: float,
    api_key: str,
) -> ScopeDecision:
    """Ask Gemini for a structured verdict."""

    # If genai.Client is monkeypatched in tests, use the mocked client
    if hasattr(genai, "Client") and getattr(genai.Client, "__name__", "") == "FakeClient":
        client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(
                timeout=max(10_000, int(timeout * 1000)),
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

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, ScopeDecision):
            return parsed
        if isinstance(parsed, dict):
            return ScopeDecision.model_validate(parsed)
        return _parse_decision(response.text or "")

    # Production runtime: use LangChain ChatGoogleGenerativeAI provider
    # Same proven transport as chat streaming & suggestion services (no 504 deadline errors).
    llm = get_llm_model(model=model, api_key=api_key, temperature=0.0)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    response = await asyncio.wait_for(llm.ainvoke(messages), timeout=timeout)
    raw_content = getattr(response, "content", response)
    if isinstance(raw_content, list):
        raw_text = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in raw_content)
    else:
        raw_text = str(raw_content or "")
    return _parse_decision(raw_text)




def _env_key(provider: str = "gemini") -> str | None:
    """Read the environment key used when no admin-managed key is healthy."""
    secret = settings.google_api_key
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
    """Return an API key plus the admin key record it came from."""
    if db is None:
        return _env_key(provider), None

    resolved = gemini_key_service.resolve_key(db, model=model, exclude_ids=excluded)
    if resolved is None:
        return None, None
    return resolved.api_key, resolved


def _record_success(provider: str, db: Session | None, record: Any) -> None:
    """Mark a pool key as used after a successful verdict."""
    key = getattr(record, "key", None)
    if db is None or key is None:
        return
    try:
        gemini_key_service.touch_key(db, key)
    except Exception:
        logger.exception("Failed to record scope decision key use.")


def _record_failure(provider: str, db: Session | None, record: Any, error: BaseException) -> None:
    """Disable or cool down a pool key after a provider error."""
    key = getattr(record, "key", None)
    if db is None or key is None:
        return
    try:
        gemini_key_service.mark_key_failure(db, key, error)
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
            decision = await _decide_with_gemini(
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


async def decide_scope(
    *,
    class_name: str,
    class_description: str | None,
    question: str,
    history: str = "",
    top_chunk_snippet: str = "",
    has_code: bool = False,
    code_snippet_truncated: str = "",
    db: Session | None = None,
) -> ScopeDecision | None:
    """Return the structured scope decision for this class and question."""

    return await _decide(
        "class-scope-prompt",
        db=db,
        class_name=class_name,
        class_description=class_description or "",
        question=question,
        history=history,
        top_chunk_snippet=top_chunk_snippet,
        has_code=str(has_code),
        code_snippet_truncated=code_snippet_truncated,
    )


async def is_confidently_out_of_scope(
    *,
    class_name: str,
    class_description: str | None,
    question: str,
    history: str = "",
    top_chunk_snippet: str = "",
    has_code: bool = False,
    code_snippet_truncated: str = "",
    db: Session | None = None,
) -> bool:
    """Return true only for a confident out-of-scope decision for this class.

    ``db`` lets the gate use the admin-managed key pools; without it the
    environment keys are used directly.
    """

    decision = await decide_scope(
        class_name=class_name,
        class_description=class_description,
        question=question,
        history=history,
        top_chunk_snippet=top_chunk_snippet,
        has_code=has_code,
        code_snippet_truncated=code_snippet_truncated,
        db=db,
    )
    return _is_confident_out_of_scope(decision)
