"""Conversation memory and follow-up resolution for contextual chat (AI-63).

The chat endpoint stays class-scoped and citation-grounded. This service adds
only the missing conversational layer:

* load the most recent complete Q&A exchanges for a session (AC-01),
* rewrite a follow-up into a standalone retrieval question (AC-02),
* render a bounded history block for the answer prompt.

Citations belong to each individual answer. History is never used as a source
for a new answer; it only resolves references such as "that deadline" before
retrieval runs (AC-03).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.config import settings
from app.core.prompt_manager import get_prompt_manager
from app.models.chat import ChatMessage, ChatSession
from app.services.llm import gateway

logger = logging.getLogger(__name__)

MAX_REWRITTEN_QUESTION_CHARS = 1200
_CITATION_MARKER = re.compile(r"\[\d+\]")


@dataclass(frozen=True)
class ConversationExchange:
    """One persisted user question and its assistant answer."""

    user: str
    assistant: str


def _message_text(content: Any) -> str:
    """Extract text from a LangChain message response."""

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


def _validate_session(
    db: Session,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    class_id: uuid.UUID | str,
) -> ChatSession:
    """Load a chat session while enforcing ownership and class scope."""

    session = db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if session is None or str(session.class_id) != str(class_id):
        # Do not reveal whether a session exists in another class.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat session not found.",
        )

    if session.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: you do not own this chat session.",
        )

    return session


def _as_exchanges(messages: Sequence[ChatMessage]) -> list[ConversationExchange]:
    """Pair USER and ASSISTANT rows in chronological order.

    Orphaned messages (for example a failed generation) are ignored so the
    model never sees half a turn as conversational context.
    """

    exchanges: list[ConversationExchange] = []
    pending_user: str | None = None

    for message in messages:
        if message.role == "USER":
            pending_user = message.content
        elif message.role == "ASSISTANT" and pending_user is not None:
            exchanges.append(ConversationExchange(user=pending_user, assistant=message.content))
            pending_user = None

    return exchanges


def _trim_to_budget(
    exchanges: Sequence[ConversationExchange],
    *,
    max_chars: int,
) -> list[ConversationExchange]:
    """Drop the oldest complete exchanges until the history fits the budget."""

    kept = list(exchanges)
    while len(kept) > 1 and sum(len(item.user) + len(item.assistant) for item in kept) > max_chars:
        kept.pop(0)
    return kept


def load_recent_exchanges(
    db: Session,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    class_id: uuid.UUID | str,
    max_exchanges: int | None = None,
    max_chars: int | None = None,
) -> list[ConversationExchange]:
    """Return the last N complete exchanges for an owned session (AC-01)."""

    _validate_session(
        db,
        session_id=session_id,
        user_id=user_id,
        class_id=class_id,
    )

    limit = max_exchanges if max_exchanges is not None else settings.chat_context_exchanges
    budget = max_chars if max_chars is not None else settings.chat_context_max_chars
    if limit <= 0:
        return []

    rows = db.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.role.in_(("USER", "ASSISTANT")),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit * 2 + 2)
    ).all()

    # Legacy rows may share a timestamp because the pair was inserted in one
    # statement. USER before ASSISTANT is a safe tie-breaker for that case.
    ordered = sorted(
        rows,
        key=lambda message: (
            message.created_at,
            0 if message.role == "USER" else 1,
        ),
    )
    exchanges = _as_exchanges(ordered)[-limit:]
    return _trim_to_budget(exchanges, max_chars=budget)


def format_history_context(
    exchanges: Sequence[ConversationExchange],
    *,
    max_chars: int | None = None,
) -> str:
    """Render history for a prompt, keeping newest turns inside a character budget."""

    if not exchanges:
        return ""

    budget = max_chars if max_chars is not None else settings.chat_context_max_chars
    blocks: list[str] = []
    used = 0

    for exchange in reversed(exchanges):
        # Old markers point at old source numbers; never let them leak into a
        # new answer whose retrieved context uses a fresh [1]..[N] numbering.
        previous_answer = _CITATION_MARKER.sub("", exchange.assistant).strip()
        block = f"Student: {exchange.user}\nAssistant: {previous_answer}"
        if blocks and used + len(block) + 2 > budget:
            break
        if not blocks and len(block) > budget:
            block = block[: max(1, budget - 1)].rstrip() + "…"
        blocks.insert(0, block)
        used += len(block) + 2

    return "\n\n".join(blocks)


def _clean_rewrite(text: str, original: str) -> str:
    """Normalize a model rewrite and reject an empty or answer-like response."""

    cleaned = text.strip().strip('"').strip("'").strip()
    if cleaned.lower().startswith("standalone question:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if not cleaned:
        return original
    return cleaned[:MAX_REWRITTEN_QUESTION_CHARS]


async def condense_question(
    history: Sequence[ConversationExchange],
    question: str,
    *,
    model: Any | None = None,
    db: Session | None = None,
    max_chars: int | None = None,
) -> str:
    """Rewrite a follow-up using recent turns (AC-02).

    The original question is the safe fallback: a missing model, a provider
    error, or an empty rewrite must never break the chat request.
    """

    if not history:
        return question

    history_text = format_history_context(history, max_chars=max_chars)
    if not history_text:
        return question

    try:
        prompt = get_prompt_manager().format(
            "condense-question-prompt",
            history=history_text,
            question=question,
        )
        messages = [
            SystemMessage(content=str(prompt["system-prompt"])),
            HumanMessage(content=str(prompt["user-prompt"])),
        ]
        if model is not None:
            response = await model.ainvoke(messages)
            rewritten = _message_text(getattr(response, "content", response))
        else:
            rewritten = await gateway.ainvoke_chat(db, messages, timeout=4.0)
    except Exception:
        logger.warning(
            "Follow-up question condensation failed; using the original question.",
            exc_info=True,
        )
        return question

    return _clean_rewrite(rewritten, question)
