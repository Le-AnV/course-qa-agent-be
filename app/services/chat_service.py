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

from app.config.config import settings
from app.core.prompt_manager import get_prompt_manager
from app.services import (
    chat_context_service,
    chat_retrieval_service,
    course_service,
    history_service,
    openai_decision_service,
    quick_response_service,
    rag_service,
    suggestion_service,
)
from app.services.chat_context_service import ConversationExchange
from app.services.llm import gateway

logger = logging.getLogger(__name__)

GENERATION_ERROR_MESSAGE = "The answer could not be generated. Please try again."
TIMEOUT_ERROR_MESSAGE = "The answer timed out. Please try again."
SUGGESTION_PREFETCH_GRACE_SECONDS = 4.0
SUGGESTION_RETRY_TIMEOUT_SECONDS = 10.0


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


async def _stream_model(llm: Any, messages: list[Any]) -> AsyncIterator[str]:
    """Stream one injected model (tests/DI); production uses the key-pool gateway."""

    async for chunk in llm.astream(messages):
        text = _chunk_text(chunk.content)
        if text:
            yield text


def _answer_stream(
    db: Session,
    model: Any | None,
    messages: list[Any],
) -> AsyncIterator[str | gateway.StreamRestart]:
    if model is not None:
        return _stream_model(model, messages)
    return gateway.astream_chat(db, messages)


def _course_context(
    chunks: Sequence[rag_service.ScoredChunk],
) -> str:
    """Build numbered context matching the citation indexes."""

    lines: list[str] = []

    for index, chunk in enumerate(chunks, start=1):
        locator = f", page {chunk.page}" if chunk.page is not None else ""
        lines.append(f"[{index}] {chunk.title}{locator}\n{chunk.content}")

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
    suggested_questions: Sequence[str] | None = None,
    used_web_search: bool = False,
    excluded_from_context: bool = False,
    thinking_ms: int | None = None,
    trace: Sequence[str] | None = None,
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
            used_web_search=used_web_search,
            citations=citations,
            suggested_questions=suggested_questions,
            excluded_from_context=excluded_from_context,
            thinking_ms=thinking_ms,
            trace=trace,
        )
    except Exception:
        logger.exception("Failed to persist chat turn for class %s.", class_id)
        return None


async def _cancel_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    """Cancel pending pre-retrieval work and suppress its cancellation errors."""

    pending = [task for task in tasks if task is not None and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def stream_chat(
    *,
    db: Session,
    message: str,
    class_id: uuid.UUID | str,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    history: Sequence[ConversationExchange] | None = None,
    model: Any | None = None,
    timeout_seconds: float | None = None,
) -> AsyncIterator[str]:
    """Retrieve class evidence and stream a grounded answer as SSE.

    ``history`` is loaded and ownership-checked by the router. The rewritten
    standalone question is used for retrieval, while the bounded history block
    is given to the answer model so follow-ups keep their conversational meaning.
    """

    context_history = list(history or ())
    logger.info(
        "Chat turn started: class=%s session=%s history=%d message_chars=%d.",
        class_id,
        session_id,
        len(context_history),
        len(message),
    )
    sources: list[rag_service.Source] = []
    answer_parts: list[str] = []
    followup_questions: list[str] = []
    suggestion_task: asyncio.Task[list[str]] | None = None
    turn_completed = False
    stage = "conversation context"
    budget = timeout_seconds if timeout_seconds is not None else settings.chat_timeout_seconds
    trace_steps: dict[str, str] = {}
    first_token_ms: int | None = None
    request_started = time.monotonic()

    def _token(text: str) -> str:
        """Stamp the first answer token so thinking time survives history reloads."""
        nonlocal first_token_ms
        if first_token_ms is None:
            first_token_ms = int((time.monotonic() - request_started) * 1000)
        return _sse("token", {"text": text})

    def _progress(stage_name: str, text: str) -> str:
        """Record the latest message per stage so the stored trace matches the UI."""
        trace_steps[stage_name] = text
        return _sse("progress", {"stage": stage_name, "message": text})

    try:
        async with asyncio.timeout(budget):
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
                    thinking_ms=0,
                    trace=[],
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
                        "user_message_id": (str(user_msg.id) if user_msg is not None else None),
                        "assistant_message_id": (
                            str(assistant_msg.id) if assistant_msg is not None else None
                        ),
                        "suggestions": [],
                        "suggested_questions": [],
                    },
                )
                return

            yield _progress("searching", "Checking class scope")
            class_record = course_service.get_class(db, class_id)
            class_name = class_record.name if class_record is not None else ""
            class_description = (
                class_record.info.description
                if class_record is not None and class_record.info is not None
                else None
            )
            scope_history = chat_context_service.format_history_context(
                context_history,
                max_chars=settings.chat_scope_history_max_chars,
            )

            # The gate and the follow-up rewrite are independent, so start both
            # immediately and bound them with one wall-clock deadline. Awaiting
            # the gate first means a refusal is never delayed by a rewrite that
            # is no longer needed.
            pre_retrieval_started = time.monotonic()
            deadline = (
                asyncio.get_running_loop().time()
                + settings.chat_pre_retrieval_timeout_seconds
            )
            gate_task = asyncio.create_task(
                openai_decision_service.is_confidently_out_of_scope(
                    class_name=class_name,
                    class_description=class_description,
                    question=message,
                    history=scope_history,
                    db=db,
                ),
                name="class-scope-gate",
            )
            rewrite_task: asyncio.Task[str] | None = None
            if context_history:
                rewrite_task = asyncio.create_task(
                    chat_context_service.condense_question(
                        context_history,
                        message,
                        model=model,
                        class_name=class_name,
                        class_description=class_description,
                        deadline=deadline,
                        db=db,
                    ),
                    name="follow-up-rewrite",
                )

            try:
                out_of_scope = False
                try:
                    async with asyncio.timeout_at(deadline):
                        out_of_scope = await gate_task
                except TimeoutError:
                    await _cancel_tasks(gate_task)
                    logger.warning(
                        "Scope gate exceeded the %.1fs pre-retrieval deadline; failing open.",
                        settings.chat_pre_retrieval_timeout_seconds,
                    )
                except Exception:
                    logger.warning(
                        "Scope gate raised unexpectedly; continuing with RAG.",
                        exc_info=True,
                    )
                    out_of_scope = False

                gate_elapsed = time.monotonic() - pre_retrieval_started

                if out_of_scope:
                    await _cancel_tasks(rewrite_task)
                    logger.info(
                        "Scope gate blocked the turn after %.2fs "
                        "(class=%s, rewrite_cancelled=%s).",
                        gate_elapsed,
                        class_id,
                        rewrite_task is not None,
                    )
                    text = (
                        "This question appears to be outside the scope of this class. "
                        "Please ask a question related to the course subject."
                    )
                    answer_parts.append(text)
                    yield _token(text)
                    saved_turn = await _persist_turn(
                        db=db,
                        user_id=user_id,
                        session_id=session_id,
                        class_id=class_id,
                        message=message,
                        answer=text,
                        citations=[],
                        excluded_from_context=True,
                        thinking_ms=first_token_ms,
                        trace=list(trace_steps.values()),
                    )
                    saved_session, user_msg, assistant_msg = saved_turn or (None, None, None)
                    yield _sse(
                        "done",
                        {
                            "source": rag_service.SOURCE_UNRELATED,
                            "used_web_search": False,
                            "threshold": 0.0,
                            "top_score": None,
                            "session_id": (
                                str(saved_session.id)
                                if saved_session is not None
                                else (str(session_id) if session_id is not None else None)
                            ),
                            "user_message_id": str(user_msg.id) if user_msg is not None else None,
                            "assistant_message_id": (
                                str(assistant_msg.id) if assistant_msg is not None else None
                            ),
                            "suggestions": [],
                            "suggested_questions": [],
                        },
                    )
                    return

                yield _progress("searching", "Searching class materials")

                standalone_question = message
                if rewrite_task is not None:
                    stage = "follow-up question resolution"
                    try:
                        async with asyncio.timeout_at(deadline):
                            standalone_question = await rewrite_task
                    except TimeoutError:
                        await _cancel_tasks(rewrite_task)
                        standalone_question = chat_context_service.qualify_with_class(
                            message, class_name, class_description
                        )
                        logger.warning(
                            "Follow-up rewrite missed the %.1fs pre-retrieval deadline; "
                            "using the class-qualified question.",
                            settings.chat_pre_retrieval_timeout_seconds,
                        )
                    except Exception:
                        logger.warning(
                            "Follow-up rewrite raised unexpectedly; using the class-qualified question.",
                            exc_info=True,
                        )
                        standalone_question = chat_context_service.qualify_with_class(
                            message, class_name, class_description
                        )
                    logger.info(
                        "Pre-retrieval finished in %.2fs "
                        "(gate=%.2fs, rewrite=yes, question_chars=%d, class=%s).",
                        time.monotonic() - pre_retrieval_started,
                        gate_elapsed,
                        len(standalone_question),
                        class_id,
                    )
                else:
                    logger.info(
                        "Pre-retrieval finished in %.2fs (gate=%.2fs, rewrite=skipped, class=%s).",
                        time.monotonic() - pre_retrieval_started,
                        gate_elapsed,
                        class_id,
                    )
            finally:
                await _cancel_tasks(gate_task, rewrite_task)

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
                yield _progress("reading", read_msg)

            if decision.source == rag_service.SOURCE_CURRICULUM:
                sources = rag_service.curriculum_citations(decision.chunks)

                if not sources:
                    text = chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE
                    answer_parts.append(text)
                    yield _token(text)
                else:
                    messages = _format_messages(
                        "answer-course-prompt",
                        context=_course_context(decision.chunks),
                        question=standalone_question,
                        history=chat_context_service.format_history_context(context_history),
                    )

                    yield _progress("drafting", "Synthesizing answer")
                    stage = "grounded answer generation"
                    stage_started = time.monotonic()
                    logger.info("Starting grounded answer generation for class %s.", class_id)
                    answer_parts: list[str] = []
                    async for part in _answer_stream(db, model, messages):
                        if isinstance(part, gateway.StreamRestart):
                            answer_parts.clear()
                            yield _sse("restart", {"message": part.message})
                        elif part:
                            answer_parts.append(part)
                            yield _token(part)
                    logger.info(
                        "Grounded answer generation completed for class %s in %.2fs.",
                        class_id,
                        time.monotonic() - stage_started,
                    )
                    # Start suggestions only once the answer is complete so
                    # they can avoid repeating what it already states.
                    suggestion_task = asyncio.create_task(
                        suggestion_service.generate_followup_questions(
                            question=standalone_question,
                            answer="".join(answer_parts),
                            chunks=decision.chunks,
                            model=model,
                            db=db,
                        )
                    )

                    # Only pages the answer actually cites, one citation per
                    # page; nothing is shown when the answer used no source.
                    for source in rag_service.cited_page_citations(
                        decision.chunks, "".join(answer_parts)
                    ):
                        yield _sse("citation", asdict(source))

            elif decision.source == rag_service.SOURCE_INTERNET:
                yield _progress("searching", "Searching the web")
                web_results = await asyncio.to_thread(
                    rag_service.search_internet, standalone_question
                )
                if not web_results:
                    text = rag_service.NO_SOURCE_MESSAGE
                    answer_parts.append(text)
                    yield _token(text)
                else:
                    sources = rag_service.internet_citations(web_results)
                    yield _progress("reading", f"Reviewing {len(web_results)} web sources")
                    context = "\n\n".join(
                        f"Source ({source.title} — {source.url}):\n{result.content}"
                        for source, result in zip(sources, web_results, strict=True)
                    )
                    messages = _format_messages(
                        "answer-internet-prompt",
                        context=context,
                        question=standalone_question,
                        history=chat_context_service.format_history_context(context_history),
                    )
                    yield _progress("drafting", "Synthesizing answer")
                    stage = "grounded answer generation"
                    stage_started = time.monotonic()
                    logger.info(
                        "Starting Internet grounded answer generation for class %s.", class_id
                    )
                    answer_parts = []
                    async for part in _answer_stream(db, model, messages):
                        if isinstance(part, gateway.StreamRestart):
                            answer_parts.clear()
                            yield _sse("restart", {"message": part.message})
                        elif part:
                            answer_parts.append(part)
                            yield _token(part)
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
                yield _token(text)

            citation_payload = [asdict(source) for source in sources]
            saved_turn = await _persist_turn(
                db=db,
                user_id=user_id,
                session_id=session_id,
                class_id=class_id,
                message=message,
                answer="".join(answer_parts),
                citations=citation_payload,
                suggested_questions=followup_questions or None,
                used_web_search=decision.source == rag_service.SOURCE_INTERNET,
                thinking_ms=first_token_ms,
                trace=list(trace_steps.values()),
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
                    "suggestions": followup_questions,
                    "suggested_questions": followup_questions,
                },
            )
            turn_completed = True

    except TimeoutError:
        if suggestion_task is not None:
            suggestion_task.cancel()
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

    if suggestion_task is None:
        return

    if not turn_completed:
        suggestion_task.cancel()
        return

    try:
        followup_questions = await asyncio.wait_for(
            suggestion_task, timeout=SUGGESTION_PREFETCH_GRACE_SECONDS
        )
    except TimeoutError:
        suggestion_task.cancel()
        followup_questions = []
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Follow-up question generation failed after done.", exc_info=True)
        return

    if not followup_questions:
        try:
            followup_questions = await asyncio.wait_for(
                suggestion_service.generate_followup_questions(
                    question=standalone_question,
                    answer="".join(answer_parts),
                    chunks=decision.chunks,
                    model=model,
                    db=db,
                ),
                timeout=SUGGESTION_RETRY_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("Follow-up question retry failed after done.", exc_info=True)
            return

    if not followup_questions:
        return

    if assistant_msg is not None:
        try:
            await asyncio.to_thread(
                history_service.update_suggested_questions,
                db=db,
                message_id=assistant_msg.id,
                questions=followup_questions,
            )
        except Exception:
            logger.warning("Failed to persist follow-up questions.", exc_info=True)

    yield _sse("suggestions", {"questions": followup_questions})
