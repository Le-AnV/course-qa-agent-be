"""Service for generating follow-up suggested questions after an answer (AI-125).

Generates 2–3 grounded follow-up questions based on the student's question,
the completed assistant response, and the retrieved course materials (AC-01 to AC-05).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.orm import Session

from app.config.config import settings
from app.core.prompt_manager import get_prompt_manager
from app.services.llm import gateway
from app.services.rag_service import ScoredChunk

logger = logging.getLogger(__name__)

MIN_FOLLOWUP_QUESTIONS = 2
MAX_FOLLOWUP_QUESTIONS = 3
MIN_CONTEXT_LENGTH = 20


def _extract_message_text(content: Any) -> str:
    """Extract displayable text from a LangChain message response."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    return ""


def _parse_json_questions(raw: str, max_count: int = 3, min_count: int = 1) -> list[str]:
    """Extract and validate a list of question strings from an LLM response."""
    cleaned = raw.strip()
    if not cleaned:
        return []

    # Strip markdown code blocks if the model wrapped output in ```json ... ```
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    # Search for JSON array brackets if extra preamble text was emitted
    match = re.search(r"\[\s*.*?\s*\]", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to decode JSON from suggestion response: %s", raw[:200])
        return []

    if not isinstance(data, list):
        return []

    questions: list[str] = []
    for item in data:
        if isinstance(item, str):
            q = item.strip().strip('"').strip("'").strip()
            if q:
                if not q.endswith("?"):
                    q = f"{q}?"
                if q not in questions:
                    questions.append(q)

    if len(questions) < min_count:
        return []

    return questions[:max_count]


async def _invoke_pool(
    db: Session | None,
    messages: list[SystemMessage | HumanMessage],
    timeout_seconds: float,
) -> str:
    """Invoke through the key-pool gateway so suggestions rotate with chat keys."""

    return await gateway.ainvoke_chat(db, messages, timeout=timeout_seconds)


async def generate_followup_questions(
    *,
    question: str,
    answer: str,
    chunks: Sequence[ScoredChunk],
    model: Any | None = None,
    db: Session | None = None,
) -> list[str]:
    """Generate 2-3 suggested follow-up questions after a completed grounded answer (AC-01 to AC-05).

    Returns an empty list when:
    - Suggestions are globally disabled
    - Query, answer, or context chunks are empty
    - Context is insufficient (AC-03)
    - Suggestion generation fails or times out (AC-05 graceful degradation)
    """
    if not settings.chat_suggestions_enabled:
        return []

    if not question.strip() or not answer.strip() or not chunks:
        return []

    # Build context from chunks
    context_parts: list[str] = []
    total_chars = 0
    for idx, chunk in enumerate(chunks[:5], start=1):
        if chunk.content and chunk.content.strip():
            part = f"[{idx}] {chunk.title}\n{chunk.content.strip()}"
            context_parts.append(part)
            total_chars += len(chunk.content.strip())

    if not context_parts or total_chars < MIN_CONTEXT_LENGTH:
        # AC-03: Not enough relevant context to support useful follow-ups
        return []

    context_text = "\n\n".join(context_parts)

    try:
        prompt = get_prompt_manager().format(
            "suggest-questions-prompt",
            context=context_text,
            question=question,
            answer=answer,
        )

        messages = [
            SystemMessage(content=str(prompt["system-prompt"])),
            HumanMessage(content=str(prompt["user-prompt"])),
        ]

        if model is not None:
            response = await asyncio.wait_for(
                model.ainvoke(messages),
                timeout=settings.chat_suggestions_timeout_seconds,
            )
            raw_output = _extract_message_text(getattr(response, "content", response))
        else:
            raw_output = await _invoke_pool(
                db,
                messages,
                timeout_seconds=settings.chat_suggestions_timeout_seconds,
            )

        # AC-01: 2-3 questions
        # AC-03: if fewer than 2 questions, return []
        return _parse_json_questions(
            raw_output,
            max_count=MAX_FOLLOWUP_QUESTIONS,
            min_count=MIN_FOLLOWUP_QUESTIONS,
        )

    except Exception:
        logger.warning(
            "Failed to generate follow-up questions; returning empty.",
            exc_info=True,
        )
        return []
