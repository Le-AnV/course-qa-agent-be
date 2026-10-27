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

import asyncio
import logging
import re
import time
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


def _remaining_timeout(deadline: float, cap: float) -> float:
    """Return the smaller of the per-call cap and the shared deadline."""

    return max(0.0, min(cap, deadline - asyncio.get_running_loop().time()))


async def _ainvoke_short(
    messages: list[Any],
    model: Any | None,
    *,
    deadline: float,
    db: Session | None = None,
) -> Any:
    """Call a small helper model within a shared wall-clock deadline.

    Tests inject their own model, so the wait/fallback path only applies to the
    production client. A provider error propagates to the caller, which decides
    whether to fall back to safe behaviour. The fallback shares the remaining
    budget instead of receiving a fresh timeout. Production calls go through the
    Gemini key-pool gateway so a failing key is rotated instead of failing the
    rewrite.
    """

    if model is not None:
        return await model.ainvoke(messages)

    cap = settings.chat_condense_timeout_seconds
    timeout = _remaining_timeout(deadline, cap)
    if timeout <= 0:
        raise TimeoutError("No time left for the follow-up rewrite.")

    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            gateway.ainvoke_chat(
                db,
                messages,
                timeout=timeout,
                allow_fallback=False,
                avoid_sticky_key=True,
            ),
            timeout=timeout,
        )
    except Exception as exc:  # any provider failure may need the fallback
        logger.warning(
            "Primary rewrite model failed after %.2fs (%s); trying fallback %s.",
            time.monotonic() - started,
            type(exc).__name__,
            settings.chat_fallback_model,
        )
        timeout = _remaining_timeout(deadline, cap)
        if timeout <= 0:
            raise TimeoutError("No time left for the fallback rewrite.") from exc
        return await asyncio.wait_for(
            gateway.ainvoke_chat(
                db,
                messages,
                model=settings.chat_fallback_model,
                timeout=timeout,
                allow_fallback=False,
                avoid_sticky_key=True,
            ),
            timeout=timeout,
        )


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
            ChatMessage.excluded_from_context.is_(False),
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


def _class_context_label(
    class_name: str | None,
    class_description: str | None,
) -> str:
    """Build a short class label from the syllabus for a failed rewrite."""

    name = " ".join((class_name or "").split())
    description = " ".join((class_description or "").split())
    if description:
        description = description[: settings.chat_class_context_max_chars].rstrip(" ,;.-")
    if name and description:
        return f"{name} — {description}"
    return name or description


def qualify_with_class(
    question: str,
    class_name: str | None,
    class_description: str | None,
) -> str:
    """Anchor a follow-up to its class subject instead of searching a bare fragment.

    The rewrite model is the preferred path. When it fails, the class name and
    description are the same scope anchor the class gate uses; appending them
    keeps pronouns and omitted subjects resolvable for retrieval and web search.
    """

    label = _class_context_label(class_name, class_description)
    if not label:
        return question

    cleaned = question.strip()
    haystack = cleaned.casefold()
    if label.casefold() in haystack:
        return cleaned
    name = " ".join((class_name or "").split())
    if name and name.casefold() in haystack:
        return cleaned

    qualified = f"{cleaned} ({label})"
    return qualified[: settings.chat_rewrite_max_chars].rstrip()


def _clean_rewrite(text: str) -> str | None:
    """Normalize a model rewrite and reject empty output."""

    cleaned = text.strip().strip('"').strip("'").strip()
    if cleaned.lower().startswith("standalone question:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if not cleaned:
        return None
    return cleaned[: settings.chat_rewrite_max_chars]


async def condense_question(
    history: Sequence[ConversationExchange],
    question: str,
    *,
    model: Any | None = None,
    db: Session | None = None,
    max_chars: int | None = None,
    class_name: str | None = None,
    class_description: str | None = None,
    deadline: float | None = None,
) -> str:
    """Rewrite a follow-up using recent turns and the class subject (AC-02).

    The rewrite is the preferred path. If the model is missing, the provider
    errors, or the response is empty, the question is qualified with the class
    name/description so retrieval and web search still receive a scoped query.
    ``deadline`` is an absolute event-loop timestamp shared with the class gate;
    the primary and fallback attempts split the remaining budget.
    """

    if not history:
        return question

    history_text = format_history_context(history, max_chars=max_chars)
    if not history_text:
        return question

    if deadline is None:
        deadline = asyncio.get_running_loop().time() + settings.chat_condense_timeout_seconds

    started = time.monotonic()
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
        else:
            response = await _ainvoke_short(messages, model, deadline=deadline, db=db)
        rewritten = _message_text(getattr(response, "content", response))
    except TimeoutError:
        logger.warning(
            "Follow-up rewrite exceeded its deadline after %.2fs; using the class-qualified question.",
            time.monotonic() - started,
        )
        return qualify_with_class(question, class_name, class_description)
    except Exception as exc:  # noqa: BLE001 - any provider failure must degrade safely
        logger.warning(
            "Follow-up rewrite failed after %.2fs (%s); using the class-qualified question.",
            time.monotonic() - started,
            type(exc).__name__,
        )
        return qualify_with_class(question, class_name, class_description)

    cleaned = _clean_rewrite(rewritten)
    if cleaned is None:
        logger.info(
            "Follow-up rewrite returned no usable text after %.2fs; using the class-qualified question.",
            time.monotonic() - started,
        )
        return qualify_with_class(question, class_name, class_description)

    logger.info(
        "Follow-up rewrite completed in %.2fs (%d -> %d chars).",
        time.monotonic() - started,
        len(question),
        len(cleaned),
    )
    return cleaned
