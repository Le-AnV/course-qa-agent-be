"""OpenAI Decisions scope check used before class document retrieval."""

from __future__ import annotations

import json
import logging

import httpx

from app.config.config import settings

logger = logging.getLogger(__name__)

DECISIONS_URL = "https://api.openai.com/v1/decisions"
DECISION_MODEL = "gpt-6-luna"
OUT_OF_SCOPE_CONFIDENCE_THRESHOLD = 0.8


async def is_confidently_out_of_scope(
    *,
    class_name: str,
    class_description: str | None,
    question: str,
    history: str,
) -> bool:
    """Return true only for a confident out-of-scope decision.

    Missing credentials, API failures, and malformed decisions fail open so
    the existing RAG flow remains available.
    """

    if settings.openai_api_key is None:
        return False

    state = json.dumps(
        {
            "class_name": class_name,
            "class_description": class_description or "",
            "recent_conversation": history,
            "question": question,
        },
        ensure_ascii=False,
    )
    payload = {
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

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0)
        ) as client:
            response = await client.post(
                DECISIONS_URL,
                headers={
                    "Authorization": (
                        f"Bearer {settings.openai_api_key.get_secret_value()}"
                    )
                },
                json=payload,
            )
            response.raise_for_status()
            answers = response.json().get("answers", [])
    except (httpx.HTTPError, ValueError, TypeError):
        logger.warning("OpenAI Decisions scope check failed; continuing with RAG.", exc_info=True)
        return False

    if not isinstance(answers, list):
        return False

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
