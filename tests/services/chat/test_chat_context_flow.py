"""End-to-end conversation memory and citation persistence for AI-63."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Generator
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401 - register all tables
from app.config.config import settings
from app.core.database import Base
from app.models.chat import ChatMessage, ChatSession
from app.schemas.history import ChatMessageResponse
from app.services import (
    chat_context_service,
    chat_retrieval_service,
    chat_service,
    history_service,
    rag_service,
)
from app.services.chat_service import stream_chat


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _disable_class_scope_gate(monkeypatch) -> None:
    """Keep these flow tests offline and focused on AI-63 follow-up behaviour."""

    async def in_scope(**kwargs) -> bool:
        return False

    monkeypatch.setattr(
        chat_service.decision_service,
        "is_confidently_out_of_scope",
        in_scope,
    )
    monkeypatch.setattr(chat_service.course_service, "get_class", lambda *args: None)


def _parse_event(raw: str) -> tuple[str, dict]:
    lines = raw.strip().splitlines()
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


async def _collect(stream) -> list[tuple[str, dict]]:
    return [_parse_event(item) async for item in stream]


def _decision(question: str) -> rag_service.RetrievalDecision:
    return rag_service.RetrievalDecision(
        question=question,
        class_id="class-a",
        threshold=0.8,
        top_score=0.91,
        source=rag_service.SOURCE_CURRICULUM,
        chunks=(
            rag_service.ScoredChunk(
                content="The database assignment deadline is Friday.",
                score=0.91,
                metadata={
                    "document_id": "policy-doc",
                    "file_name": "policy.pdf",
                    "page_number": 3,
                },
            ),
        ),
    )


def _existing_session(db: Session, user_id: uuid.UUID, class_id: uuid.UUID) -> ChatSession:
    session = ChatSession(user_id=user_id, class_id=class_id, title="Policy questions")
    db.add(session)
    db.commit()
    db.refresh(session)
    history_service.save_chat_turn(
        db=db,
        user_id=user_id,
        class_id=class_id,
        user_content="What is the database assignment policy?",
        assistant_content="The policy explains the assignment deadline.",
        session_id=session.id,
    )
    return session


def test_follow_up_uses_history_and_persists_citations(db_session: Session, monkeypatch) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()
    session = _existing_session(db_session, user_id, class_id)
    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=user_id,
        class_id=class_id,
    )
    captured: dict[str, object] = {}

    class FakeModel:
        async def ainvoke(self, messages):
            prompt_text = "\n".join(str(message.content) for message in messages)
            if "database assignment policy" in prompt_text or "standalone" in prompt_text.lower():
                captured["rewrite_prompt"] = prompt_text
                return SimpleNamespace(content="When is the database assignment deadline?")
            return SimpleNamespace(content="[]")

        async def astream(self, messages):
            captured["answer_prompt"] = "\n".join(str(message.content) for message in messages)
            yield SimpleNamespace(content="The database assignment deadline is Friday [1].")

    def retrieve(db, *, question, class_id):
        captured["retrieval_question"] = question
        return _decision(question)

    monkeypatch.setattr(chat_retrieval_service, "retrieve_for_chat", retrieve)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="So when is the deadline?",
                class_id=class_id,
                user_id=user_id,
                session_id=session.id,
                history=history,
                model=FakeModel(),
                timeout_seconds=1,
            )
        )
    )

    assert captured["retrieval_question"] == "When is the database assignment deadline?"
    assert "database assignment policy" in str(captured["rewrite_prompt"])
    assert "database assignment policy" in str(captured["answer_prompt"])
    assert "When is the database assignment deadline?" in str(captured["answer_prompt"])

    done = events[-1]
    assert done[0] == "done"
    assert done[1]["session_id"] == str(session.id)
    assert done[1]["user_message_id"] is not None
    assert done[1]["assistant_message_id"] is not None

    rows = db_session.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.asc())
    ).all()
    assert len(rows) == 4
    assert rows[2].role == "USER"
    assert rows[2].content == "So when is the deadline?"
    assert rows[3].role == "ASSISTANT"
    assert rows[3].content == "The database assignment deadline is Friday [1]."
    assert rows[3].citations == [
        {
            "index": 1,
            "kind": "curriculum",
            "title": "policy.pdf",
            "url": None,
            "snippet": "The database assignment deadline is Friday.",
            "document_id": "policy-doc",
            "page": 3,
            "indexes": [],
            "highlights": [],
        }
    ]

    response = ChatMessageResponse.model_validate(rows[3])
    assert response.citations[0].title == "policy.pdf"


def test_refused_turn_is_persisted_but_not_replayed_as_context(
    db_session: Session, monkeypatch
) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()
    session = _existing_session(db_session, user_id, class_id)

    async def out_of_scope(**kwargs) -> bool:
        return True

    monkeypatch.setattr(
        chat_service.decision_service,
        "is_confidently_out_of_scope",
        out_of_scope,
    )
    monkeypatch.setattr(
        chat_service.course_service,
        "get_class",
        lambda *args: SimpleNamespace(
            name="Database Systems",
            info=SimpleNamespace(description="Relational databases and SQL."),
        ),
    )

    events = asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="Giá vàng hôm nay bao nhiêu?",
                class_id=class_id,
                user_id=user_id,
                session_id=session.id,
                timeout_seconds=1,
            )
        )
    )

    assert events[-1][0] == "done"
    assert events[-1][1]["source"] == rag_service.SOURCE_UNRELATED

    rows = db_session.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.asc())
    ).all()
    refused = [row for row in rows if row.excluded_from_context]
    assert len(refused) == 2
    assert {row.role for row in refused} == {"USER", "ASSISTANT"}

    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=user_id,
        class_id=class_id,
    )
    assert [exchange.user for exchange in history] == [
        "What is the database assignment policy?"
    ]


def test_failed_rewrite_qualifies_the_question_with_the_class(
    db_session: Session, monkeypatch
) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()
    session = _existing_session(db_session, user_id, class_id)
    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=user_id,
        class_id=class_id,
    )
    captured: dict[str, str] = {}

    class BrokenRewriteModel:
        async def ainvoke(self, messages):
            raise RuntimeError("provider unavailable")

        async def astream(self, messages):
            yield SimpleNamespace(content="Một con trỏ lưu địa chỉ bộ nhớ [1].")

    def retrieve(db, *, question, class_id):
        captured["retrieval_question"] = question
        return _decision(question)

    monkeypatch.setattr(chat_retrieval_service, "retrieve_for_chat", retrieve)
    monkeypatch.setattr(
        chat_service.course_service,
        "get_class",
        lambda *args: SimpleNamespace(
            name="C++ - Toturial",
            info=SimpleNamespace(description="Con trỏ, OOP và STL."),
        ),
    )

    asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="Cho t ví dụ đi",
                class_id=class_id,
                user_id=user_id,
                session_id=session.id,
                history=history,
                model=BrokenRewriteModel(),
                timeout_seconds=1,
            )
        )
    )

    assert captured["retrieval_question"] == (
        "Cho t ví dụ đi (C++ - Toturial — Con trỏ, OOP và STL)"
    )


def test_first_turn_creates_a_session_and_returns_its_id(db_session: Session, monkeypatch) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()

    class FakeModel:
        async def astream(self, messages):
            yield SimpleNamespace(content="The policy is in the syllabus [1].")

    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda db, *, question, class_id: _decision(question),
    )

    events = asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="What is the database policy?",
                class_id=class_id,
                user_id=user_id,
                model=FakeModel(),
                timeout_seconds=1,
            )
        )
    )

    done = events[-1]
    assert done[0] == "done"
    assert done[1]["session_id"] is not None
    session = db_session.get(ChatSession, uuid.UUID(done[1]["session_id"]))
    assert session is not None
    assert session.user_id == user_id
    assert session.class_id == class_id
    assert session.title == "What is the database policy?"
    assert done[1]["assistant_message_id"] is not None


def _internet_decision(question: str) -> rag_service.RetrievalDecision:
    return rag_service.RetrievalDecision(
        question=question,
        class_id="class-a",
        threshold=0.8,
        top_score=0.55,
        source=rag_service.SOURCE_INTERNET,
    )


def test_internet_without_results_still_persists_the_turn(db_session: Session, monkeypatch) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()

    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda db, *, question, class_id: _internet_decision(question),
    )
    monkeypatch.setattr(rag_service, "search_internet", lambda question: [])

    events = asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="What is the refund policy?",
                class_id=class_id,
                user_id=user_id,
                timeout_seconds=1,
            )
        )
    )

    done = events[-1]
    assert done[0] == "done"
    assert done[1]["session_id"] is not None
    session = db_session.get(ChatSession, uuid.UUID(done[1]["session_id"]))
    assert session is not None
    assert session.title == "What is the refund policy?"

    rows = db_session.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.asc())
    ).all()
    assert [row.role for row in rows] == ["USER", "ASSISTANT"]
    assert rows[1].content == rag_service.NO_SOURCE_MESSAGE


def test_internet_answer_persists_streamed_text_and_citations(
    db_session: Session, monkeypatch
) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()

    class FakeModel:
        async def astream(self, messages):
            yield SimpleNamespace(content="Refunds take five business days [1].")

    web_results = [
        rag_service.WebResult(
            title="Refund policy",
            url="https://example.com/refund",
            content="Refunds take five business days.",
        )
    ]

    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda db, *, question, class_id: _internet_decision(question),
    )
    monkeypatch.setattr(rag_service, "search_internet", lambda question: web_results)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="How long do refunds take?",
                class_id=class_id,
                user_id=user_id,
                model=FakeModel(),
                timeout_seconds=1,
            )
        )
    )

    done = events[-1]
    assert done[0] == "done"
    assert done[1]["session_id"] is not None
    session = db_session.get(ChatSession, uuid.UUID(done[1]["session_id"]))
    assert session is not None

    rows = db_session.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.asc())
    ).all()
    assert [row.role for row in rows] == ["USER", "ASSISTANT"]
    assert rows[1].content == "Refunds take five business days [1]."
    assert rows[1].citations[0]["kind"] == rag_service.SOURCE_INTERNET
    assert rows[1].citations[0]["url"] == "https://example.com/refund"


def test_internet_follow_up_keeps_history_and_searches_the_standalone_question(
    db_session: Session, monkeypatch
) -> None:
    """Internet answers must keep the conversational layer that AI-63 added."""

    user_id = uuid.uuid4()
    class_id = uuid.uuid4()
    session = _existing_session(db_session, user_id, class_id)
    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=user_id,
        class_id=class_id,
    )
    captured: dict[str, str] = {}

    class FakeModel:
        async def ainvoke(self, messages):
            captured["rewrite_prompt"] = "\n".join(str(message.content) for message in messages)
            return SimpleNamespace(
                content="How long do refunds take for a cancelled course?"
            )

        async def astream(self, messages):
            captured["answer_prompt"] = "\n".join(str(message.content) for message in messages)
            yield SimpleNamespace(content="Refunds take five business days.")

    def search(question: str) -> list[rag_service.WebResult]:
        captured["search_question"] = question
        return [
            rag_service.WebResult(
                title="Refund policy",
                url="https://example.com/refund",
                content="Refunds take five business days.",
            )
        ]

    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda db, *, question, class_id: _internet_decision(question),
    )
    monkeypatch.setattr(rag_service, "search_internet", search)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="How long does that take?",
                class_id=class_id,
                user_id=user_id,
                session_id=session.id,
                history=history,
                model=FakeModel(),
                timeout_seconds=1,
            )
        )
    )

    standalone = "How long do refunds take for a cancelled course?"
    assert captured["search_question"] == standalone
    assert "database assignment policy" in str(captured["rewrite_prompt"])
    assert "database assignment policy" in captured["answer_prompt"]
    assert standalone in captured["answer_prompt"]

    done = events[-1]
    assert done[0] == "done"
    assert done[1]["source"] == rag_service.SOURCE_INTERNET
    assert done[1]["used_web_search"] is True

    rows = db_session.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.asc())
    ).all()
    # The user's original wording is persisted, not the rewritten question.
    assert rows[2].content == "How long does that take?"
    # The history flag must match what the stream reported, otherwise a reload
    # shows an internet answer as curriculum-sourced.
    assert rows[3].used_web_search is True


def _class_record():
    return SimpleNamespace(
        name="C++ - Toturial",
        info=SimpleNamespace(description="Con trỏ, OOP và STL."),
    )


class _AnswerModel:
    async def astream(self, messages):
        yield SimpleNamespace(content="Câu trả lời [1].")


def test_gate_and_rewrite_run_in_parallel(db_session: Session, monkeypatch) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()
    session = _existing_session(db_session, user_id, class_id)
    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=user_id,
        class_id=class_id,
    )
    marks: dict[str, float] = {}

    async def gate(**kwargs) -> bool:
        marks["gate_start"] = time.monotonic()
        await asyncio.sleep(0.2)
        marks["gate_end"] = time.monotonic()
        return False

    async def rewrite(*args, **kwargs) -> str:
        marks["rewrite_start"] = time.monotonic()
        await asyncio.sleep(0.2)
        marks["rewrite_end"] = time.monotonic()
        return "When is the database assignment deadline?"

    monkeypatch.setattr(chat_service.decision_service, "is_confidently_out_of_scope", gate)
    monkeypatch.setattr(chat_context_service, "condense_question", rewrite)
    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda db, *, question, class_id: _decision(question),
    )

    asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="So when is the deadline?",
                class_id=class_id,
                user_id=user_id,
                session_id=session.id,
                history=history,
                model=_AnswerModel(),
                timeout_seconds=1,
            )
        )
    )

    # The gate must still be running when the rewrite starts, and vice versa.
    assert marks["rewrite_start"] < marks["gate_end"]
    assert marks["gate_start"] < marks["rewrite_end"]


def test_gate_block_returns_immediately_and_cancels_the_rewrite(
    db_session: Session, monkeypatch
) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()
    session = _existing_session(db_session, user_id, class_id)
    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=user_id,
        class_id=class_id,
    )
    rewrites: dict[str, object] = {"cancelled": False}

    async def gate(**kwargs) -> bool:
        await asyncio.sleep(0.05)
        return True

    async def slow_rewrite(*args, **kwargs) -> str:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            rewrites["cancelled"] = True
            raise
        return "must not be used"

    monkeypatch.setattr(chat_service.decision_service, "is_confidently_out_of_scope", gate)
    monkeypatch.setattr(chat_context_service, "condense_question", slow_rewrite)

    started = time.monotonic()
    events = asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="Giá vàng hôm nay bao nhiêu?",
                class_id=class_id,
                user_id=user_id,
                session_id=session.id,
                history=history,
                timeout_seconds=1,
            )
        )
    )
    elapsed = time.monotonic() - started

    assert events[-1][0] == "done"
    assert events[-1][1]["source"] == rag_service.SOURCE_UNRELATED
    # The refusal must not wait for the rewrite that is no longer needed.
    assert elapsed < 2
    assert rewrites["cancelled"] is True


def test_rewrite_that_misses_the_deadline_uses_the_class_qualified_question(
    db_session: Session, monkeypatch
) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()
    session = _existing_session(db_session, user_id, class_id)
    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=user_id,
        class_id=class_id,
    )
    captured: dict[str, str] = {}

    async def gate(**kwargs) -> bool:
        return False

    async def hanging_rewrite(*args, **kwargs) -> str:
        await asyncio.sleep(5)
        return "too late"

    def retrieve(db, *, question, class_id):
        captured["retrieval_question"] = question
        return _decision(question)

    monkeypatch.setattr(chat_service.decision_service, "is_confidently_out_of_scope", gate)
    monkeypatch.setattr(chat_context_service, "condense_question", hanging_rewrite)
    monkeypatch.setattr(chat_retrieval_service, "retrieve_for_chat", retrieve)
    monkeypatch.setattr(chat_service.course_service, "get_class", lambda *args: _class_record())
    monkeypatch.setattr(settings, "chat_pre_retrieval_timeout_seconds", 0.2)

    asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="Cho t ví dụ đi",
                class_id=class_id,
                user_id=user_id,
                session_id=session.id,
                history=history,
                model=_AnswerModel(),
                timeout_seconds=1,
            )
        )
    )

    assert captured["retrieval_question"] == (
        "Cho t ví dụ đi (C++ - Toturial — Con trỏ, OOP và STL)"
    )


def test_gate_that_misses_the_deadline_fails_open(db_session: Session, monkeypatch) -> None:
    user_id = uuid.uuid4()
    class_id = uuid.uuid4()
    session = _existing_session(db_session, user_id, class_id)
    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=user_id,
        class_id=class_id,
    )
    captured: dict[str, str] = {}
    gates: dict[str, object] = {"cancelled": False}

    async def slow_gate(**kwargs) -> bool:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            gates["cancelled"] = True
            raise
        return True

    async def rewrite(*args, **kwargs) -> str:
        return "When is the database assignment deadline?"

    def retrieve(db, *, question, class_id):
        captured["retrieval_question"] = question
        return _decision(question)

    monkeypatch.setattr(
        chat_service.decision_service, "is_confidently_out_of_scope", slow_gate
    )
    monkeypatch.setattr(chat_context_service, "condense_question", rewrite)
    monkeypatch.setattr(chat_retrieval_service, "retrieve_for_chat", retrieve)
    monkeypatch.setattr(settings, "chat_pre_retrieval_timeout_seconds", 0.2)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=db_session,
                message="So when is the deadline?",
                class_id=class_id,
                user_id=user_id,
                session_id=session.id,
                history=history,
                model=_AnswerModel(),
                timeout_seconds=1,
            )
        )
    )

    assert events[-1][0] == "done"
    assert events[-1][1]["source"] == rag_service.SOURCE_CURRICULUM
    assert captured["retrieval_question"] == "When is the database assignment deadline?"
    # A hung gate is abandoned so it cannot block retrieval.
    assert gates["cancelled"] is True
