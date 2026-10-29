"""Service for generating follow-up suggested questions after an answer (AI-125).

Generates 2–3 grounded follow-up questions based on the student's question,
the conversation history, and the retrieved course materials (AC-01 to AC-05).
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

MIN_FOLLOWUP_QUESTIONS = 1
MAX_FOLLOWUP_QUESTIONS = 3
MIN_CONTEXT_LENGTH = 10


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


def build_fallback_followups(
    *,
    question: str,
    class_name: str = "",
    chunks: Sequence[ScoredChunk] = (),
) -> list[str]:
    """Generate 2-3 logical follow-up questions from available material context."""
    results: list[str] = []

    # Check if chunks have titles or topic keywords
    for chunk in chunks[:3]:
        title = (chunk.title or "").replace(".pdf", "").replace(".docx", "").strip()
        if title and title.lower() not in {"course material", "unknown"}:
            results.append(f"Bạn có thể giải thích thêm về nội dung trong phần '{title}' không?")
            if len(results) >= 2:
                break

    q_lower = question.lower()
    if "con trỏ" in q_lower or "pointer" in q_lower or "ptr" in q_lower:
        results.append("Sự khác biệt giữa con trỏ (pointer) và tham chiếu (reference) trong C++ là gì?")
        results.append("Làm thế nào để tránh lỗi con trỏ treo (dangling pointer) và rò rỉ bộ nhớ?")
    elif "hàm" in q_lower or "function" in q_lower or "đệ quy" in q_lower or "recursion" in q_lower:
        results.append("Điều kiện dừng (base case) quan trọng như thế nào trong hàm đệ quy?")
        results.append("Ưu và nhược điểm giữa đệ quy và vòng lặp lặp lại (iteration) là gì?")
    elif "chữ hoa" in q_lower or "case" in q_lower or "cú pháp" in q_lower or "syntax" in q_lower:
        results.append("Quy ước đặt tên biến và hàm phổ biến (naming conventions) trong C++ là gì?")
        results.append("C++ có những từ khóa dành riêng (reserved keywords) nào cần tránh khi đặt tên?")
    else:
        topic = class_name or "môn học này"
        results.append(f"Những ứng dụng thực tế phổ biến nhất của chủ đề này trong {topic} là gì?")
        results.append("Có những lỗi thường gặp (common pitfalls) nào cần lưu ý về phần này?")

    unique: list[str] = []
    for q in results:
        if q not in unique:
            unique.append(q)
    return unique[:3]


async def _invoke_pool(
    db: Session | None,
    messages: list[SystemMessage | HumanMessage],
    timeout_seconds: float,
) -> str:
    """Invoke through the key-pool gateway so suggestions rotate with chat keys."""

    return await gateway.ainvoke_chat(
        db,
        messages,
        timeout=timeout_seconds,
        avoid_sticky_key=True,
    )


async def generate_followup_questions(
    *,
    question: str,
    answer: str = "",
    chunks: Sequence[ScoredChunk] = (),
    history: str = "",
    model: Any | None = None,
    db: Session | None = None,
) -> list[str]:
    """Generate 2-3 suggested follow-up questions grounded in course materials and chat history."""
    if not settings.chat_suggestions_enabled:
        return []

    cleaned_q = question.strip()
    if not cleaned_q:
        return []

    # Build context from materials (chunks)
    context_parts: list[str] = []
    for idx, chunk in enumerate(chunks[:5], start=1):
        if chunk.content and chunk.content.strip():
            part = f"[{idx}] {chunk.title}\n{chunk.content.strip()}"
            context_parts.append(part)

    if not context_parts:
        context_parts.append(f"Course Material & Concepts:\n{cleaned_q}")

    context_text = "\n\n".join(context_parts)

    try:
        prompt = get_prompt_manager().format(
            "suggest-questions-prompt",
            context=context_text,
            history=history or "(No previous conversation)",
            question=cleaned_q,
            answer=answer.strip()
            or (
                "Not available yet - this suggestion is generated in parallel with "
                "the answer. Base the questions on the Course Context and User Question."
            ),
        )

        messages = [
            SystemMessage(content=str(prompt["system-prompt"])),
            HumanMessage(content=str(prompt["user-prompt"])),
        ]

        timeout_sec = max(settings.chat_suggestions_timeout_seconds, 8.0)
        async with asyncio.timeout(timeout_sec):
            if model is not None:
                response = await model.ainvoke(messages)
                raw_output = _extract_message_text(getattr(response, "content", response))
            else:
                raw_output = await _invoke_pool(
                    db,
                    messages,
                    timeout_seconds=timeout_sec,
                )

        parsed = _parse_json_questions(
            raw_output,
            max_count=MAX_FOLLOWUP_QUESTIONS,
            min_count=MIN_FOLLOWUP_QUESTIONS,
        )
        if parsed:
            return parsed
        return build_fallback_followups(question=cleaned_q, chunks=chunks)

    except Exception:
        logger.warning(
            "Failed to generate follow-up questions from model; using material fallback.",
            exc_info=True,
        )
        return build_fallback_followups(question=cleaned_q, chunks=chunks)
