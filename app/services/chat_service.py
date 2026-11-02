"""SSE streaming service for class-scoped question answering (AI-61)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
    decision_service,
    document_service,
    history_service,
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
SUGGESTION_RETRY_TIMEOUT_SECONDS = 15.0


def _format_syllabus_roadmap(
    db: Session, class_id: uuid.UUID | str
) -> tuple[str, rag_service.ScoredChunk | None]:
    """Load and format the structured syllabus roadmap from database if available."""
    try:
        doc, outline, units = document_service.get_class_syllabus_roadmap(db, class_id)
        if not units and not outline:
            return "", None

        parts: list[str] = ["### Structured Course Syllabus Roadmap (Authoritative Source):"]
        doc_name = doc.file_name if doc and doc.file_name else "Course Syllabus"
        doc_id = str(doc.id) if doc and doc.id else ""
        parts.append(f"- Source Document: {doc_name}")
        if outline:
            parts.append(f"- Topic Outline: {outline.strip()}")
        if units:
            parts.append(f"- Total Units: {len(units)}")
            for u in units:
                desc = f" - {u.description.strip()}" if u.description else ""
                parts.append(f"  * Unit {u.unit_number}: {u.unit_title.strip()}{desc}")
                if u.content and u.content.strip():
                    for line in u.content.strip().splitlines():
                        if line.strip():
                            parts.append(f"      {line.strip()}")

        formatted_text = "\n".join(parts)
        chunk = rag_service.ScoredChunk(
            content=formatted_text,
            score=1.0,
            metadata={
                "title": doc_name,
                "file_name": doc_name,
                "document_id": doc_id,
                "page": None,
            },
        )
        return formatted_text, chunk
    except Exception:
        logger.warning(
            "Failed to load structured syllabus roadmap for class %s",
            class_id,
            exc_info=True,
        )
        return "", None

_HOMEWORK_PATTERNS = re.compile(
    r"(?:"
    r"bài\s*tập|"
    r"homework|"
    r"assignment|"
    r"lab\s*\d|"
    r"(?:làm|giải|viết)\s*(?:hộ|giúp|dùm)|"
    r"viết\s*code\s*(?:giúp|hộ|dùm)|"
    r"nộp\s*(?:bài|lab|assignment)|"
    r"mai\s*nộp|"
    r"cho\s*(?:mình|em)\s*(?:xin\s*)?code|"
    r"code\s*(?:mẫu\s*hoàn\s*chỉnh|giúp|hộ|dùm)|"
    r"(?:làm|giải)\s*bài"
    r")",
    re.IGNORECASE,
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

    return "\n\n".join(
        rag_service._curriculum_context_line(index, chunk)
        for index, chunk in enumerate(chunks, start=1)
    )


def _with_syllabus(
    context: str,
    class_description: str | None,
    syllabus_roadmap: str = "",
) -> str:
    """Prepend the class syllabus summary and structured roadmap so overview questions can be answered."""

    sections: list[str] = []
    roadmap = (syllabus_roadmap or "").strip()
    if roadmap:
        sections.append(roadmap)

    description = (class_description or "").strip()
    if description:
        sections.append(f"Course syllabus summary (not a numbered source):\n{description}")

    ctx = (context or "").strip()
    if ctx:
        sections.append(ctx)

    return "\n\n".join(sections) if sections else context


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


_ORIGINAL_IS_OUT_OF_SCOPE = decision_service.is_confidently_out_of_scope


async def _resolve_scope_decision(
    *,
    class_name: str,
    class_description: str | None,
    question: str,
    history: str = "",
    top_chunk_snippet: str = "",
    has_code: bool = False,
    code_snippet_truncated: str = "",
    db: Session | None = None,
) -> decision_service.ScopeDecision | None:
    if decision_service.is_confidently_out_of_scope is not _ORIGINAL_IS_OUT_OF_SCOPE:
        blocked = await decision_service.is_confidently_out_of_scope(
            class_name=class_name,
            class_description=class_description,
            question=question,
            history=history,
            top_chunk_snippet=top_chunk_snippet,
            has_code=has_code,
            code_snippet_truncated=code_snippet_truncated,
            db=db,
        )
        if blocked:
            return decision_service.ScopeDecision(
                verdict=decision_service.ScopeVerdict.OUT_OF_SCOPE.value,
                confidence=1.0,
            )
        return decision_service.ScopeDecision(
            verdict=decision_service.ScopeVerdict.CORE.value,
            confidence=1.0,
        )

    return await decision_service.decide_scope(
        class_name=class_name,
        class_description=class_description,
        question=question,
        history=history,
        top_chunk_snippet=top_chunk_snippet,
        has_code=has_code,
        code_snippet_truncated=code_snippet_truncated,
        db=db,
    )


def _determine_chat_action(
    *,
    has_code: bool,
    scope_decision: decision_service.ScopeDecision | None,
    decision_chunks: Sequence[rag_service.ScoredChunk],
    is_high_zone: bool,
    is_mid_zone: bool,
    is_low_zone: bool,
    is_hw: bool,
    confidence_threshold: float = 0.8,
    has_roadmap: bool = False,
) -> str:
    """Resolve the pipeline action based on the refined Truth Table."""
    raw_verdict = scope_decision.verdict if scope_decision else None
    verdict = (
        raw_verdict.value if hasattr(raw_verdict, "value") else str(raw_verdict)
    ).lower() if raw_verdict is not None else None
    gate_low_confidence = (
        scope_decision is None
        or scope_decision.confidence < confidence_threshold
    )

    if is_hw:
        return "code_help" if has_code else "docs_socratic"

    if has_code:
        # OUT_OF_SCOPE was already rejected beforehand. Code queries route to code_help.
        return "code_help"
    elif gate_low_confidence:
        # Gate error / timeout: fail-safe to avoid unverified web search
        if is_high_zone or is_mid_zone or has_roadmap:
            return "docs"
        return "safe_fallback"
    elif verdict == decision_service.ScopeVerdict.LOGISTICS.value:
        # Administrative questions use course materials only (no web search);
        # the prompt reminds students to confirm specifics on LMS.
        return "docs" if (decision_chunks or has_roadmap) else "safe_fallback"
    elif verdict == decision_service.ScopeVerdict.COMPARISON.value:
        if is_low_zone and not decision_chunks:
            return "web_search"
        return "docs_plus_web"
    elif verdict == decision_service.ScopeVerdict.EXTENSION.value:
        if is_high_zone:
            return "docs"
        elif is_mid_zone:
            return "docs_plus_web"
        return "web_search"
    elif verdict == decision_service.ScopeVerdict.CORE.value:
        if is_high_zone:
            return "docs"
        elif is_mid_zone:
            return "docs_plus_web"
        return "docs" if has_roadmap else "web_search"
    else:
        if is_high_zone:
            return "docs"
        elif is_mid_zone:
            return "docs_plus_web"
        return "docs" if has_roadmap else "web_search"



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
    chunks_for_suggestions: Sequence[rag_service.ScoredChunk] = ()
    suggestion_task: asyncio.Task[list[str]] | None = None
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

            yield _sse(
                "progress",
                {"stage": "searching", "message": "Checking class scope"},
            )
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

            clean_message, student_code, has_code = decision_service.preprocess_student_query(message)
            pre_retrieval_started = time.monotonic()
            deadline = (
                asyncio.get_running_loop().time()
                + settings.chat_pre_retrieval_timeout_seconds
            )
            gate_question = clean_message if clean_message else "Analyze student code"
            scope_decision: decision_service.ScopeDecision | None = None
            gate_task: asyncio.Task[decision_service.ScopeDecision | None] | None = None

            if has_code:
                # Fast path: queries containing code/compiler errors are directly in-scope code help
                scope_decision = decision_service.ScopeDecision(
                    verdict=decision_service.ScopeVerdict.CORE.value,
                    confidence=1.0,
                )
            else:
                gate_task = asyncio.create_task(
                    _resolve_scope_decision(
                        class_name=class_name,
                        class_description=class_description,
                        question=gate_question,
                        history=scope_history,
                        has_code=False,
                        code_snippet_truncated="",
                        db=db,
                    ),
                    name="class-scope-gate",
                )

            rewrite_task: asyncio.Task[str] | None = None
            if context_history and clean_message:
                rewrite_task = asyncio.create_task(
                    chat_context_service.condense_question(
                        context_history,
                        clean_message,
                        model=model,
                        class_name=class_name,
                        class_description=class_description,
                        deadline=deadline,
                        db=db,
                    ),
                    name="follow-up-rewrite",
                )

            try:
                if gate_task is not None:
                    try:
                        async with asyncio.timeout_at(deadline):
                            scope_decision = await gate_task
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
                        scope_decision = None

                gate_elapsed = time.monotonic() - pre_retrieval_started

                if (
                    scope_decision is not None
                    and scope_decision.verdict == decision_service.ScopeVerdict.OUT_OF_SCOPE.value
                    and scope_decision.confidence >= settings.decision_confidence_threshold
                ):
                    await _cancel_tasks(rewrite_task)
                    logger.info(
                        "Scope gate blocked the turn after %.2fs "
                        "(class=%s, rewrite_cancelled=%s).",
                        gate_elapsed,
                        class_id,
                        rewrite_task is not None,
                    )
                    if scope_decision.is_related_tech:
                        text = (
                            f"This question appears to be outside the scope of this class "
                            f"(related to other technology). Please ask a question related to {class_name}."
                        )
                    else:
                        text = (
                            "This question appears to be outside the scope of this class. "
                            "Please ask a question related to the course subject."
                        )
                    answer_parts.append(text)
                    yield _sse("token", {"text": text})
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

                yield _sse(
                    "progress", {"stage": "searching", "message": "Searching class materials"}
                )

                standalone_question = clean_message if clean_message else "Explain and debug the student code:"
                if rewrite_task is not None:
                    stage = "follow-up question resolution"
                    try:
                        async with asyncio.timeout_at(deadline):
                            standalone_question = await rewrite_task
                    except TimeoutError:
                        await _cancel_tasks(rewrite_task)
                        standalone_question = chat_context_service.qualify_with_class(
                            clean_message, class_name, class_description
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
                            clean_message, class_name, class_description
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
            retrieval_query = standalone_question
            if not retrieval_query.strip() or retrieval_query == "Explain and debug the student code:":
                retrieval_query = student_code[:300].strip() if has_code else standalone_question

            logger.info("Starting class retrieval and reranking for class %s (query_chars=%d).", class_id, len(retrieval_query))
            search_query_en = scope_decision.search_query_en if scope_decision else None
            retrieval_kwargs: dict[str, Any] = {"question": retrieval_query, "class_id": class_id}
            if search_query_en is not None:
                try:
                    import inspect
                    sig = inspect.signature(chat_retrieval_service.retrieve_for_chat)
                    if "search_query_en" in sig.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                    ):
                        retrieval_kwargs["search_query_en"] = search_query_en
                except Exception:
                    logger.debug("Could not pass search_query_en to retrieve_for_chat", exc_info=True)

            decision = await asyncio.to_thread(
                chat_retrieval_service.retrieve_for_chat,
                db,
                **retrieval_kwargs,
            )
            logger.info(
                "Class retrieval and reranking completed for class %s in %.2fs; chunks=%d.",
                class_id,
                time.monotonic() - stage_started,
                len(decision.chunks),
            )

            is_high_zone = decision.top_score is not None and decision.top_score >= decision.threshold
            is_mid_zone = (
                decision.top_score is not None
                and decision.unrelated_threshold <= decision.top_score < decision.threshold
            )
            is_low_zone = not is_high_zone and not is_mid_zone
            is_hw = bool(scope_decision and scope_decision.is_homework_request) or bool(
                _HOMEWORK_PATTERNS.search(message)
            )
            verdict = scope_decision.verdict if scope_decision else None
            gate_low_confidence = (
                scope_decision is None
                or scope_decision.confidence < settings.decision_confidence_threshold
            )

            roadmap_text, roadmap_chunk = _format_syllabus_roadmap(db, class_id)
            has_roadmap = bool(roadmap_text)

            action = _determine_chat_action(
                has_code=has_code,
                scope_decision=scope_decision,
                decision_chunks=decision.chunks,
                is_high_zone=is_high_zone,
                is_mid_zone=is_mid_zone,
                is_low_zone=is_low_zone,
                is_hw=is_hw,
                confidence_threshold=settings.decision_confidence_threshold,
                has_roadmap=has_roadmap,
            )

            # Phase 1: Context & Prompt Resolution
            fallback_text: str | None = None
            reading_message: str = ""
            messages: list[Any] | None = None
            slide_chunks: list[rag_service.ScoredChunk] = []
            web_results: list[rag_service.WebResult] = []

            if action in ("docs", "docs_socratic"):
                if decision.chunks:
                    doc_titles = list(dict.fromkeys(c.title for c in decision.chunks if c.title))
                    count = len(decision.chunks)
                    unit = "section" if count == 1 else "sections"
                    if doc_titles:
                        primary = doc_titles[0]
                        if len(primary) > 24:
                            primary = primary[:21] + "..."
                        reading_message = (
                            f"Found {count} {unit} in {primary}"
                            if len(doc_titles) == 1
                            else f"Found {count} {unit} in {primary} & {len(doc_titles) - 1} more"
                        )
                    else:
                        reading_message = f"Found {count} relevant {unit}"

                    slide_chunks = list(decision.chunks)
                    if roadmap_chunk is not None:
                        slide_chunks = [roadmap_chunk] + [
                            c for c in decision.chunks
                            if c.document_id != roadmap_chunk.document_id
                        ]
                    messages = _format_messages(
                        "answer-course-prompt",
                        context=_with_syllabus(
                            _course_context(slide_chunks),
                            class_description,
                            syllabus_roadmap=roadmap_text,
                        ),
                        question=standalone_question,
                        history=chat_context_service.format_history_context(context_history),
                    )
                    if verdict == decision_service.ScopeVerdict.LOGISTICS.value and not is_hw:
                        messages[0] = SystemMessage(
                            content=str(messages[0].content)
                            + "\n\nADMINISTRATIVE NOTE: Answer only from the provided course materials. "
                            "Never invent exam dates, deadlines or grading details. If specific dates are not in the "
                            "materials, say so and remind the student to check official LMS announcements or the instructor. "
                            "Answer in the language of the student."
                        )
                    if action == "docs_socratic" or is_hw:
                        messages[0] = SystemMessage(
                            content=(
                            "You are an Academic Socratic Tutor for this course.\n"
                            "The student is asking for help with their homework, lab assignment, or asking you to write code for an exercise.\n\n"
                            "STRICT PEDAGOGICAL / ANTI-CHEATING RULES:\n"
                            "1. ABSOLUTELY NO COMPLETE CODE: Under NO circumstances should you provide complete, copy-pasteable, runnable code or full function implementations (such as a complete recursive factorial function or main() method). Never write the final answer for the student.\n"
                            "2. Concept Explanation: Explain mathematical concepts clearly. NEVER use dollar signs ($) or LaTeX math delimiters ($...$ or $...$). Write all formulas, equations, and variables as plain text or inline code in backticks (e.g. `n! = n * (n-1)!`, `0! = 1`, `F(0) = 0`).\n"
                            "3. Socratic Breakdown: Guide the student step-by-step through the thought process:\n"
                            "   - What is the Base Case (điều kiện dừng để không bị tràn stack / lặp vô tận)?\n"
                            "   - What is the Recursive Step (công thức truy hồi để đưa bài toán về kích thước nhỏ hơn)?\n"
                            "4. Pseudocode & Skeleton: Provide pseudocode, algorithm logic, or a code skeleton with blanks/TODO comments only (e.g. `// TODO: viết điều kiện dừng tại đây`).\n"
                            "5. Guiding Questions: Ask 1-2 targeted Socratic questions encouraging the student to implement the missing parts and reply with their solution.\n"
                            "6. Citations: Reference the provided Course Materials [1..N] where relevant. If not available, rely on standard computer science principles without apologizing or saying documents are missing.\n"
                            "7. Tone & Language: Enthusiastic, supportive, pedagogical. Always respond in the language of the student (Vietnamese)."
                        )
                        )
                elif roadmap_chunk is not None:
                    reading_message = f"Found syllabus roadmap in {roadmap_chunk.title}"
                    slide_chunks = [roadmap_chunk]
                    messages = _format_messages(
                        "answer-course-prompt",
                        context=_with_syllabus(
                            _course_context([roadmap_chunk]),
                            class_description,
                            syllabus_roadmap="",
                        ),
                        question=standalone_question,
                        history=chat_context_service.format_history_context(context_history),
                    )
                    if verdict == decision_service.ScopeVerdict.LOGISTICS.value and not is_hw:
                        messages[0] = SystemMessage(
                            content=str(messages[0].content)
                            + "\n\nADMINISTRATIVE NOTE: Answer only from the provided course materials. "
                            "Never invent exam dates, deadlines or grading details. If specific dates are not in the "
                            "materials, say so and remind the student to check official LMS announcements or the instructor. "
                            "Answer in the language of the student."
                        )
                else:
                    if action == "docs_socratic" or is_hw:
                        reading_message = "Applying Socratic pedagogical guidance"
                        slide_chunks = []
                        messages = _format_messages(
                            "answer-course-prompt",
                            context="Course syllabus & standard computer science concepts.",
                            question=standalone_question,
                            history=chat_context_service.format_history_context(context_history),
                        )
                        messages[0] = SystemMessage(
                            content=(
                            "You are an Academic Socratic Tutor for this course.\n"
                            "The student is asking for help with their homework, lab assignment, or asking you to write code for an exercise.\n\n"
                            "STRICT PEDAGOGICAL / ANTI-CHEATING RULES:\n"
                            "1. ABSOLUTELY NO COMPLETE CODE: Under NO circumstances should you provide complete, copy-pasteable, runnable code or full function implementations (such as a complete recursive factorial function or main() method). Never write the final answer for the student.\n"
                            "2. Concept Explanation: Explain mathematical concepts clearly. NEVER use dollar signs ($) or LaTeX math delimiters ($...$ or $...$). Write all formulas, equations, and variables as plain text or inline code in backticks (e.g. `n! = n * (n-1)!`, `0! = 1`, `F(0) = 0`).\n"
                            "3. Socratic Breakdown: Guide the student step-by-step through the thought process:\n"
                            "   - What is the Base Case (điều kiện dừng để không bị tràn stack / lặp vô tận)?\n"
                            "   - What is the Recursive Step (công thức truy hồi để đưa bài toán về kích thước nhỏ hơn)?\n"
                            "4. Pseudocode & Skeleton: Provide pseudocode, algorithm logic, or a code skeleton with blanks/TODO comments only (e.g. `// TODO: viết điều kiện dừng tại đây`).\n"
                            "5. Guiding Questions: Ask 1-2 targeted Socratic questions encouraging the student to implement the missing parts and reply with their solution.\n"
                            "6. Citations: Reference the provided Course Materials [1..N] where relevant. If not available, rely on standard computer science principles without apologizing or saying documents are missing.\n"
                            "7. Tone & Language: Enthusiastic, supportive, pedagogical. Always respond in the language of the student (Vietnamese)."
                        )
                        )
                    else:
                        fallback_text = chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE

            elif action == "code_help":
                reading_message = "Analyzing student code"
                slide_chunks = list(decision.chunks) if decision.chunks else []
                if not slide_chunks and not is_hw:
                    try:
                        search_q = f"{class_name} C++ pointer reference" if ("ptr" in student_code or "*" in student_code) else f"{class_name} {standalone_question or 'programming syntax reference'}"
                        web_results = await asyncio.to_thread(rag_service.search_internet, search_q)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Web search for code help failed: %s", exc)
                        web_results = []
                slide_context = _course_context(decision.chunks) if decision.chunks else ""
                code_block = f"### Student Code / Error Trace:\n```\n{student_code}\n```"
                full_context = f"{slide_context}\n\n{code_block}" if slide_context else code_block

                messages = _format_messages(
                    "answer-course-prompt",
                    context=full_context,
                    question=standalone_question,
                    history=chat_context_service.format_history_context(context_history),
                )
                if is_hw:
                    messages[0] = SystemMessage(
                        content=(
                            "You are an Academic Assistant specializing in code explanation and tutoring for this course.\n"
                            "The student provided code for a potential homework or assignment.\n"
                            "RULES:\n"
                            "1. Socratic Guidance: Point out logical errors, explain memory and runtime behavior, but DO NOT provide the complete final solution code. Guide them with pseudocode and hints.\n"
                            "2. Citations: Reference the provided Course Materials [1..N] where relevant.\n"
                            "3. Language: Answer in the language of the student."
                        )
                    )
                else:
                    messages[0] = SystemMessage(
                        content=(
                            "You are an Academic Assistant specializing in code explanation and debugging for this course.\n"
                            "Your task is to thoroughly explain, trace, and debug the student's code snippet or compiler error.\n"
                            "RULES:\n"
                            "1. Comprehensive Analysis: Explain step-by-step what the code does, how variables and memory (e.g. pointers, addresses, values) change at each step, and what the output will be.\n"
                            "2. Debugging & Best Practices: Point out any potential bugs, undefined behaviors (e.g. uninitialized pointers, memory leaks), and modern best practices.\n"
                            "3. Citations: Cite the provided Course Materials [1..N] where relevant.\n"
                            "4. Do NOT refuse: Do NOT say 'there is no information in the course context'. Always explain the code thoroughly.\n"
                            "5. Language: Answer in the language of the student."
                        )
                    )

            elif action == "docs_plus_web":
                web_query = (
                    scope_decision.external_search_query
                    if scope_decision and scope_decision.external_search_query
                    else standalone_question
                )
                yield _progress("searching", f"Searching comparison: {web_query}")
                web_results = await asyncio.to_thread(rag_service.search_internet, web_query)

                slide_chunks = list(decision.chunks) if decision.chunks else []
                if not web_results and not slide_chunks:
                    fallback_text = rag_service.NO_SOURCE_MESSAGE
                else:
                    slide_context = _course_context(decision.chunks) if decision.chunks else "(No course slides matched)"
                    preview_slide_citations = rag_service.curriculum_citations(slide_chunks) if slide_chunks else []
                    preview_web_citations = (
                        rag_service.internet_citations(web_results, start_index=len(preview_slide_citations) + 1)
                        if web_results
                        else []
                    )
                    web_context = (
                        "\n\n".join(
                            f"[{source.index}] ({source.title}):\n{result.content}"
                            for source, result in zip(preview_web_citations, web_results, strict=True)
                        )
                        if web_results
                        else "(No external search results found)"
                    )
                    combined_context = f"### Course Materials:\n{slide_context}\n\n### External Technology Sources:\n{web_context}"

                    reading_message = f"Reviewing {len(preview_slide_citations) + len(preview_web_citations)} combined sources"
                    messages = _format_messages(
                        "answer-course-prompt",
                        context=combined_context,
                        question=standalone_question,
                        history=chat_context_service.format_history_context(context_history),
                    )
                    messages[0] = SystemMessage(
                        content=(
                            "You are an Academic Assistant embedded in an Educational RAG system.\n"
                            "Your task is to provide a comprehensive, insightful, and well-structured Markdown comparison "
                            "between the course subject and external technologies/concepts.\n\n"
                            "RULES:\n"
                            "1. Synthesis & Depth: Thoroughly explain and compare both sides across essential aspects (such as syntax, compilation/execution model, performance, and practical use cases). "
                            "Synthesize information from the provided Course Materials and External Technology Sources, and integrate standard computer science / programming knowledge to deliver a complete, highly informative answer. "
                            "Do NOT refuse to answer, do NOT say 'không có thông tin chi tiết', and do NOT output generic disclaimer boilerplates.\n"
                            "2. Citations & Links: Cite course materials as [1..N] where relevant. For external technology points, cite [N+1..] or mention the technology/source naturally. "
                            "Do NOT output raw URLs, markdown hyperlinks (e.g. [title](url)), or bare lists of links at the end, because sources are already displayed as interactive badges in the user interface.\n"
                            "3. Formatting: Structure clearly with Markdown headings, a side-by-side comparison summary table or bullet points, and code snippets when comparing syntax.\n"
                            "4. Language: Answer in the language of the User Question."
                        )
                    )
                    if is_hw:
                        messages[0] = SystemMessage(
                            content=str(messages[0].content)
                            + "\n\nSOCRATIC TUTORING RULE: Explain trade-offs and concepts step-by-step with hints rather than solving homework directly."
                        )

            elif action == "web_search":
                web_query = (
                    scope_decision.external_search_query
                    if (scope_decision and scope_decision.external_search_query)
                    else (
                        scope_decision.search_query_en
                        if (scope_decision and scope_decision.search_query_en)
                        else standalone_question
                    )
                )
                is_unverified = gate_low_confidence
                gap_priority = "low" if is_unverified else ("high" if is_low_zone else "medium")
                logger.info(
                    "Content Gap logged for class=%s question='%s' web_query='%s' verdict=%s score=%s priority=%s unverified=%s",
                    class_id,
                    standalone_question,
                    web_query,
                    verdict,
                    decision.top_score,
                    gap_priority,
                    is_unverified,
                )
                yield _progress("searching", "Searching the web")
                web_results = await asyncio.to_thread(
                    rag_service.search_internet, web_query
                )
                if not web_results:
                    if decision.chunks:
                        slide_chunks = list(decision.chunks)
                        reading_message = f"Found {len(slide_chunks)} course sections (web search returned no results)"
                        messages = _format_messages(
                            "answer-course-prompt",
                            context=_course_context(decision.chunks),
                            question=standalone_question,
                            history=chat_context_service.format_history_context(context_history),
                        )
                        if is_hw:
                            messages[0] = SystemMessage(
                                content=str(messages[0].content)
                                + "\n\nSOCRATIC TUTORING RULE: Guide the student with hints and pseudocode."
                            )
                    else:
                        fallback_text = rag_service.NO_SOURCE_MESSAGE
                else:
                    preview_web_citations = rag_service.internet_citations(web_results)
                    reading_message = f"Reviewing {len(web_results)} web sources"
                    context = "\n\n".join(
                        f"Source ({source.title}):\n{result.content}"
                        for source, result in zip(preview_web_citations, web_results, strict=True)
                    )
                    messages = _format_messages(
                        "answer-internet-prompt",
                        context=context,
                        question=standalone_question,
                        history=chat_context_service.format_history_context(context_history),
                    )
                    if is_hw:
                        messages[0] = SystemMessage(
                            content=str(messages[0].content)
                            + "\n\nSOCRATIC TUTORING RULE: Guide the student with conceptual hints rather than providing full homework answers."
                        )

            else:  # safe_fallback
                fallback_text = chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE

            # Phase 2: Unified Execution (Generation & Follow-up Suggestions)
            if fallback_text is not None:
                answer_parts.append(fallback_text)
                yield _token(fallback_text)
            elif messages is not None:
                if reading_message:
                    yield _progress("reading", reading_message)
                yield _progress("drafting", "Synthesizing answer")

                # Prepare suggestion context for all answer actions.
                code_fallback_chunks = (
                    [
                        rag_service.ScoredChunk(
                            content=f"Student Code Context:\n{student_code}\n\nAnalyzing concepts, memory behavior, and logic.",
                            score=1.0,
                            metadata={"title": "Student Code"},
                        )
                    ]
                    if has_code and student_code
                    else []
                )
                chunks_for_suggestions = (
                    slide_chunks
                    if slide_chunks
                    else (
                        [
                            rag_service.ScoredChunk(
                                content=r.content,
                                score=1.0,
                                metadata={"title": r.title, "url": r.url},
                            )
                            for r in web_results
                        ]
                        if web_results
                        else code_fallback_chunks
                    )
                )
                suggestion_question = (
                    f"Explain and debug: {student_code[:120]}"
                    if (not clean_message and has_code and student_code)
                    else standalone_question
                )

                stage = f"{action} answer generation"
                stage_started = time.monotonic()
                logger.info("Starting %s for class %s.", stage, class_id)
                answer_parts = []
                async for part in _answer_stream(db, model, messages):
                    if isinstance(part, gateway.StreamRestart):
                        answer_parts.clear()
                        yield _sse("restart", {"message": part.message})
                    elif part:
                        answer_parts.append(part)
                        yield _token(part)
                logger.info(
                    "%s completed for class %s in %.2fs.",
                    stage,
                    class_id,
                    time.monotonic() - stage_started,
                )

                # Use the completed answer to avoid repeating its content.
                suggestion_task = asyncio.create_task(
                    suggestion_service.generate_followup_questions(
                        question=suggestion_question,
                        answer="".join(answer_parts),
                        chunks=chunks_for_suggestions,
                        history=chat_context_service.format_history_context(context_history),
                        model=model,
                        db=db,
                    )
                )

            # Phase 3: Unified Post-Processing (Citations with highlights)
            full_answer = "".join(answer_parts)
            sources = []

            if slide_chunks and full_answer and full_answer not in (
                chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE,
                rag_service.NO_SOURCE_MESSAGE,
            ):
                page_citations = rag_service.cited_page_citations(
                    slide_chunks,
                    full_answer,
                    question=f"{standalone_question}\n{search_query_en or ''}",
                )
                if not page_citations:
                    page_citations = rag_service.curriculum_citations(slide_chunks)
                sources.extend(page_citations)

            if web_results and full_answer and full_answer != rag_service.NO_SOURCE_MESSAGE:
                web_citations = rag_service.internet_citations(
                    web_results, start_index=len(sources) + 1
                )
                used_markers = rag_service.cited_markers(full_answer)
                matched_web = [s for s in web_citations if s.index in used_markers]
                sources.extend(matched_web if matched_web else web_citations)

            if not sources and full_answer and full_answer not in (
                chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE,
                rag_service.NO_SOURCE_MESSAGE,
            ):
                if decision.chunks:
                    sources.extend(rag_service.curriculum_citations(decision.chunks))
                elif slide_chunks:
                    sources.extend(rag_service.curriculum_citations(slide_chunks))
                elif not is_hw and action not in ("docs", "docs_socratic"):
                    try:
                        web_ref = await asyncio.to_thread(
                            rag_service.search_internet, f"{class_name} {standalone_question[:80]}"
                        )
                        if web_ref:
                            sources.extend(rag_service.internet_citations(web_ref[:2]))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Web reference search failed: %s", exc)
                if not sources and class_record is not None:
                    sources.append(
                        rag_service.Source(
                            index=1,
                            kind=rag_service.SOURCE_CURRICULUM,
                            title=class_record.name or f"{class_name} Material",
                            url=None,
                            snippet=f"Course syllabus and material for {class_name}",
                            document_id=None,
                            page=1,
                            indexes=(1,),
                            highlights=(),
                        )
                    )

            for source in sources:
                yield _sse("citation", asdict(source))

            # Await suggestions before sending done event
            if suggestion_task is not None:
                try:
                    followup_questions = await asyncio.wait_for(
                        suggestion_task, timeout=SUGGESTION_PREFETCH_GRACE_SECONDS
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Suggestion prefetch wait timed out or failed: %s", exc)
                    if suggestion_task and not suggestion_task.done():
                        suggestion_task.cancel()
                    followup_questions = []

            # Guaranteed follow-up questions
            if not followup_questions and full_answer and full_answer not in (
                chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE,
                rag_service.NO_SOURCE_MESSAGE,
            ):
                followup_questions = suggestion_service.build_fallback_followups(
                    question=message,
                    class_name=class_name,
                    chunks=chunks_for_suggestions,
                )

            if followup_questions:
                yield _sse("suggestions", {"questions": followup_questions})

            citation_payload = [asdict(source) for source in sources]
            used_web = action in ("web_search", "docs_plus_web") or decision.source == rag_service.SOURCE_INTERNET
            saved_turn = await _persist_turn(
                db=db,
                user_id=user_id,
                session_id=session_id,
                class_id=class_id,
                message=message,
                answer="".join(answer_parts),
                citations=citation_payload,
                suggested_questions=followup_questions or None,
                used_web_search=used_web,
                thinking_ms=first_token_ms,
                trace=list(trace_steps.values()),
            )
            saved_session, user_msg, assistant_msg = saved_turn or (None, None, None)

            yield _sse(
                "done",
                {
                    "source": (
                        rag_service.SOURCE_CURRICULUM
                        if action in ("docs", "docs_socratic", "code_help")
                        else (
                            "docs_plus_web"
                            if action == "docs_plus_web"
                            else (
                                rag_service.SOURCE_INTERNET
                                if action == "web_search"
                                else decision.source
                            )
                        )
                    ),
                    "action": action,
                    "gate_verdict": verdict,
                    "has_code": has_code,
                    "is_homework": is_hw,
                    "used_web_search": used_web,
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
