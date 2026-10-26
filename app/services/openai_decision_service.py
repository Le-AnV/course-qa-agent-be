"""OpenAI Decisions scope check used before class document retrieval."""

from __future__ import annotations

import json
import logging
import uuid

import httpx
from sqlalchemy.orm import Session

from app.config.config import settings  # noqa: F401  (tests patch this attribute)
from app.services import openai_key_service

logger = logging.getLogger(__name__)

DECISIONS_URL = "https://api.openai.com/v1/decisions"
DECISION_MODEL = "gpt-6-luna"
OUT_OF_SCOPE_CONFIDENCE_THRESHOLD = 0.8
MAX_KEY_ATTEMPTS = 3


async def is_confidently_out_of_scope(
    *,
    class_name: str,
    class_description: str | None,
    question: str,
    history: str,
    db: Session | None = None,
    auto_switch: bool = True,
) -> bool:
    """Return true only for a confident out-of-scope decision.

    Keys come from the admin-managed OpenAI pool first, then from the
    ``OPENAI_API_KEY`` environment fallback. A rejected key is disabled, a
    rate-limited key cools down, and the next healthy key is tried. Missing
    credentials, API failures, and malformed decisions fail open so the
    existing RAG flow remains available.
    """

    payload = _payload(
        class_name=class_name,
        class_description=class_description,
        question=question,
        history=history,
    )
    excluded: set[uuid.UUID] = set()

    for _ in range(MAX_KEY_ATTEMPTS):
        resolved = openai_key_service.resolve_key(db, exclude_ids=excluded)
        if resolved is None:
            return False

        try:
            answers = await _request_decisions(resolved.api_key, payload)
        except (httpx.HTTPError, ValueError, TypeError) as error:
            logger.warning(
                "OpenAI Decisions scope check failed on key %s; trying the next key.",
                resolved.key_name,
                exc_info=True,
            )
            _record_failure(db, resolved, error)
            if not auto_switch or resolved.key_id is None:
                return False
            excluded.add(resolved.key_id)
            continue

        _record_success(db, resolved)
        return _is_out_of_scope(answers)

    return False


def _payload(
    *,
    class_name: str,
    class_description: str | None,
    question: str,
    history: str,
) -> dict:
    state = json.dumps(
        {
            "class_name": class_name,
            "class_description": class_description or "",
            "recent_conversation": history,
            "question": question,
        },
        ensure_ascii=False,
    )
    return {
        "model": DECISION_MODEL,
        "input": state,
        "questions": [
            {
                "type": "choice",
                "name": "scope",
                "instructions": (
                    "Judge scope against the specific subject taught in this class, using "
                    "the class name and description. Do not treat a related field or shared "
                    "skill as in scope by itself. For example, in a C++ Basic class, a question "
                    "specifically about Python is out_of_scope unless the class description says "
                    "Python is covered. Use recent conversation only to resolve short follow-up "
                    "wording; it does not broaden the class scope. Treat ambiguous questions "
                    "about the established class subject as in_scope."
                ),
                "choices": [
                    {
                        "value": "in_scope",
                        "description": "The question directly concerns the class subject or a topic explicitly listed as covered.",
                    },
                    {
                        "value": "out_of_scope",
                        "description": "The question asks about another subject or language not listed as covered, even if broadly related.",
                    },
                ],
            }
        ],
    }


async def _request_decisions(api_key: str, payload: dict) -> list:
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        response = await client.post(
            DECISIONS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        response.raise_for_status()
    answers = response.json().get("answers", [])
    if not isinstance(answers, list):
        raise TypeError("OpenAI Decisions returned malformed answers.")
    return answers


def _record_failure(db: Session | None, resolved, error: BaseException) -> None:
    if db is None or resolved.key is None:
        return
    try:
        openai_key_service.mark_key_failure(db, resolved.key, error)
    except Exception:
        logger.exception("Failed to record OpenAI key failure for %s.", resolved.key_id)


def _record_success(db: Session | None, resolved) -> None:
    if db is None or resolved.key is None:
        return
    try:
        openai_key_service.touch_key(db, resolved.key)
    except Exception:
        logger.exception("Failed to mark OpenAI key %s as used.", resolved.key_id)


def _is_out_of_scope(answers: list) -> bool:
    answer = next(
        (
            item
            for item in answers
            if isinstance(item, dict)
            and item.get("type") == "choice"
            and item.get("name") == "scope"
        ),
        None,
    )
    if answer is None:
        return False

    confidence = answer.get("confidence")
    return (
        answer.get("choice") == "out_of_scope"
        and isinstance(confidence, (int, float))
        and confidence >= OUT_OF_SCOPE_CONFIDENCE_THRESHOLD
    )
