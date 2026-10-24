"""SSE streaming service for class-scoped question answering (AI-61)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.orm import Session

from app.core.prompt_manager import get_prompt_manager
from app.services import chat_context_service, chat_retrieval_service, history_service, rag_service
from app.services.chat_context_service import ConversationExchange
from app.services.llm.provider import get_llm_model

logger = logging.getLogger(__name__)

CHAT_TIMEOUT_SECONDS = 300.0

GENERATION_ERROR_MESSAGE = (
    "The answer could not be generated. Please try again."
)
TIMEOUT_ERROR_MESSAGE = (
    "The answer timed out. Please try again."
)


def _sse(event: str, data: dict[str, Any]) -> str:
    """Serialize one Server-Sent Event."""

    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _chunk_text(content: Any) -> str:
    """Extract displayable text from a LangChain message chunk."""

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


def _course_context(
    chunks: Sequence[rag_service.ScoredChunk],
) -> str:
    """Build numbered context matching the citation indexes."""

    lines: list[str] = []

    for index, chunk in enumerate(chunks, start=1):
        locator = (
            f", page {chunk.page}"
            if chunk.page is not None
            else ""
        )
        lines.append(
            f"[{index}] {chunk.title}{locator}\n{chunk.content}"
        )

    return "\n\n".join(lines)


def _format_messages(
    prompt_name: str,
    *,
    context: str,
    question: str,
    history: str = "",
) -> list[SystemMessage | HumanMessage]:
    """Render the existing project prompt for Gemini."""

    prompt = get_prompt_manager().format(
        prompt_name,
        context=context,
        question=question,
        history=history or "(No previous conversation.)",
    )

    return [
        SystemMessage(content=str(prompt["system-prompt"])),
        HumanMessage(content=str(prompt["user-prompt"])),
    ]


async def _persist_turn(
    *,
    db: Session,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    class_id: uuid.UUID | str,
    message: str,
    answer: str,
    citations: Sequence[dict[str, Any]],
) -> tuple[Any, Any, Any] | None:
    """Save a completed turn without letting a DB failure break the SSE stream."""

    if user_id is None or not answer.strip():
        return None

    try:
        return await asyncio.to_thread(
            history_service.save_chat_turn,
            db=db,
            user_id=user_id,
            class_id=class_id,
            user_content=message,
            assistant_content=answer,
            session_id=session_id,
            used_web_search=False,
            citations=citations,
        )
    except Exception:
        logger.exception("Failed to persist chat turn for class %s.", class_id)
        return None


async def stream_chat(
    *,
    db: Session,
    message: str,
    class_id: uuid.UUID | str,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    history: Sequence[ConversationExchange] | None = None,
    model: Any | None = None,
    timeout_seconds: float = CHAT_TIMEOUT_SECONDS,
) -> AsyncIterator[str]:
    """Retrieve class evidence and stream a grounded answer as SSE.

    ``history`` is loaded and ownership-checked by the router. The rewritten
    standalone question is used for retrieval, while the bounded history block
    is given to the answer model so follow-ups keep their conversational meaning.
    """

    context_history = list(history or ())
    sources: list[rag_service.Source] = []
    answer_parts: list[str] = []
    stage = "conversation context"

    try:
        async with asyncio.timeout(timeout_seconds):
            yield _sse("progress", {"stage": "searching"})

            standalone_question = message
            if context_history:
                stage = "follow-up question resolution"
                standalone_question = await chat_context_service.condense_question(
                    context_history,
                    message,
                    model=model,
                )
                if standalone_question != message:
                    logger.info(
                        "Resolved follow-up for class %s using %d previous exchanges.",
                        class_id,
                        len(context_history),
                    )

            stage = "class retrieval and reranking"
            stage_started = time.monotonic()
            logger.info("Starting class retrieval and reranking for class %s.", class_id)
            decision = await asyncio.to_thread(
                chat_retrieval_service.retrieve_for_chat,
                db,
                question=standalone_question,
                class_id=class_id,
            )
            logger.info(
                "Class retrieval and reranking completed for class %s in %.2fs; chunks=%d.",
                class_id,
                time.monotonic() - stage_started,
                len(decision.chunks),
            )
            if decision.chunks:
                yield _sse("progress", {"stage": "reading"})

            if decision.source == rag_service.SOURCE_CURRICULUM:
                llm = model or get_llm_model()
                sources = rag_service.curriculum_citations(decision.chunks)

                if not sources:
                    text = chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE
                    answer_parts.append(text)
                    yield _sse("token", {"text": text})
                else:
                    messages = _format_messages(
                        "answer-course-prompt",
                        context=_course_context(decision.chunks),
                        question=standalone_question,
                        history=chat_context_service.format_history_context(context_history),
                    )

                    yield _sse("progress", {"stage": "drafting"})
                    stage = "grounded answer generation"
                    stage_started = time.monotonic()
                    logger.info("Starting grounded answer generation for class %s.", class_id)
                    async for chunk in llm.astream(messages):
                        text = _chunk_text(chunk.content)
                        if text:
                            answer_parts.append(text)
                            yield _sse("token", {"text": text})
                    logger.info(
                        "Grounded answer generation completed for class %s in %.2fs.",
                        class_id,
                        time.monotonic() - stage_started,
                    )

                    for source in sources:
                        yield _sse("citation", asdict(source))

            else:
                text = chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE
                answer_parts.append(text)
                yield _sse("token", {"text": text})

            citation_payload = [asdict(source) for source in sources]
            saved_turn = await _persist_turn(
                db=db,
                user_id=user_id,
                session_id=session_id,
                class_id=class_id,
                message=message,
                answer="".join(answer_parts),
                citations=citation_payload,
            )
            saved_session, user_msg, assistant_msg = saved_turn or (None, None, None)

            yield _sse(
                "done",
                {
                    "source": decision.source,
                    "used_web_search": False,
                    "threshold": decision.threshold,
                    "top_score": decision.top_score,
                    "session_id": (
                        str(saved_session.id)
                        if saved_session is not None
                        else (str(session_id) if session_id is not None else None)
                    ),
                    "user_message_id": str(user_msg.id) if user_msg is not None else None,
                    "assistant_message_id": (
                        str(assistant_msg.id) if assistant_msg is not None else None
                    ),
                },
            )

    except TimeoutError:
        logger.warning(
            "Chat request timed out during %s for class %s.",
            stage,
            class_id,
        )
        yield _sse(
            "error",
            {"message": TIMEOUT_ERROR_MESSAGE},
        )

    except Exception:
        logger.exception(
            "Chat stream failed for class %s.",
            class_id,
        )
        yield _sse(
            "error",
            {"message": GENERATION_ERROR_MESSAGE},
        )
