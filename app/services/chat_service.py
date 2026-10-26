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
from app.services import (
    chat_context_service,
    chat_retrieval_service,
    history_service,
    quick_response_service,
    rag_service,
)
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


ENABLE_QUOTA_FALLBACK: bool = True
FALLBACK_MODEL_NAME: str = "models/gemini-2.5-flash"
PRIMARY_MODEL_FIRST_CHUNK_TIMEOUT: float = 12.0


async def _stream_with_fallback(
    llm: Any,
    messages: list[Any],
    *,
    allow_fallback: bool = True,
    first_chunk_timeout: float = PRIMARY_MODEL_FIRST_CHUNK_TIMEOUT,
) -> AsyncIterator[str]:
    """Stream chunks from LLM, falling back if 429 quota or connection hang occurs."""
    if not (ENABLE_QUOTA_FALLBACK and allow_fallback):
        async for chunk in llm.astream(messages):
            text = _chunk_text(chunk.content)
            if text:
                yield text
        return

    stream_iter = llm.astream(messages).__aiter__()
    try:
        first_chunk = await asyncio.wait_for(
            stream_iter.__anext__(),
            timeout=first_chunk_timeout,
        )
        text = _chunk_text(first_chunk.content)
        if text:
            yield text
        async for chunk in stream_iter:
            text = _chunk_text(chunk.content)
            if text:
                yield text
        return
    except StopAsyncIteration:
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Primary LLM (%s) failed or timed out (%s). Falling back to %s.",
            getattr(llm, "model", "primary"),
            exc,
            FALLBACK_MODEL_NAME,
        )
        fallback = get_llm_model(model=FALLBACK_MODEL_NAME)
        async for chunk in fallback.astream(messages):
            text = _chunk_text(chunk.content)
            if text:
                yield text


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
            quick_answer = quick_response_service.get_quick_response(message)
            if quick_answer:
                chunks = quick_response_service.split_into_stream_chunks(quick_answer)
                for chunk in chunks:
                    yield _sse("token", {"text": chunk})
                    await asyncio.sleep(0.02)

                saved_turn = await _persist_turn(
                    db=db,
                    user_id=user_id,
                    session_id=session_id,
                    class_id=class_id,
                    message=message,
                    answer=quick_answer,
                    citations=[],
                )
                saved_session, user_msg, assistant_msg = saved_turn or (None, None, None)

                yield _sse(
                    "done",
                    {
                        "source": "conversation",
                        "used_web_search": False,
                        "threshold": 0.0,
                        "top_score": None,
                        "session_id": (
                            str(saved_session.id)
                            if saved_session is not None
                            else (str(session_id) if session_id is not None else None)
                        ),
                        "user_message_id": (
                            str(user_msg.id) if user_msg is not None else None
                        ),
                        "assistant_message_id": (
                            str(assistant_msg.id) if assistant_msg is not None else None
                        ),
                    },
                )
                return

            yield _sse("progress", {"stage": "searching", "message": "Searching class materials"})

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
            if decision.chunks and decision.source == rag_service.SOURCE_CURRICULUM:
                doc_titles = list(dict.fromkeys(c.title for c in decision.chunks if c.title))
                count = len(decision.chunks)
                unit = "section" if count == 1 else "sections"
                if doc_titles:
                    primary = doc_titles[0]
                    if len(primary) > 24:
                        primary = primary[:21] + "..."
                    if len(doc_titles) == 1:
                        read_msg = f"Found {count} {unit} in {primary}"
                    else:
                        read_msg = f"Found {count} {unit} in {primary} & {len(doc_titles) - 1} more"
                else:
                    read_msg = f"Found {count} relevant {unit}"
                yield _sse("progress", {"stage": "reading", "message": read_msg})

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

                    yield _sse("progress", {"stage": "drafting", "message": "Synthesizing answer"})
                    stage = "grounded answer generation"
                    stage_started = time.monotonic()
                    logger.info("Starting grounded answer generation for class %s.", class_id)
                    answer_parts: list[str] = []
                    async for text in _stream_with_fallback(llm, messages, allow_fallback=(model is None)):
                        if text:
                            answer_parts.append(text)
                            yield _sse("token", {"text": text})
                    logger.info(
                        "Grounded answer generation completed for class %s in %.2fs.",
                        class_id,
                        time.monotonic() - stage_started,
                    )

                    # Only pages the answer actually cites, one citation per
                    # page; nothing is shown when the answer used no source.
                    for source in rag_service.cited_page_citations(
                        decision.chunks, "".join(answer_parts)
                    ):
                        yield _sse("citation", asdict(source))

            elif decision.source == rag_service.SOURCE_INTERNET:
                yield _sse("progress", {"stage": "searching", "message": "Searching the web"})
                web_results = await asyncio.to_thread(rag_service.search_internet, message)
                if not web_results:
                    text = rag_service.NO_SOURCE_MESSAGE
                    answer_parts.append(text)
                    yield _sse("token", {"text": text})
                else:
                    llm = model or get_llm_model()
                    sources = rag_service.internet_citations(web_results)
                    yield _sse("progress", {"stage": "reading", "message": f"Reviewing {len(web_results)} web sources"})
                    context = "\n\n".join(
                        f"Source ({source.title} — {source.url}):\n{result.content}"
                        for source, result in zip(sources, web_results, strict=True)
                    )
                    messages = _format_messages(
                        "answer-internet-prompt",
                        context=context,
                        question=message,
                    )
                    yield _sse("progress", {"stage": "drafting", "message": "Synthesizing answer"})
                    stage = "grounded answer generation"
                    stage_started = time.monotonic()
                    logger.info("Starting Internet grounded answer generation for class %s.", class_id)
                    answer_parts = []
                    async for text in _stream_with_fallback(llm, messages, allow_fallback=(model is None)):
                        if text:
                            answer_parts.append(text)
                            yield _sse("token", {"text": text})
                    logger.info(
                        "Internet answer generation completed for class %s in %.2fs.",
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
                    "used_web_search": decision.source == rag_service.SOURCE_INTERNET,
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
