"""End-to-end conversation memory and citation persistence for AI-63."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Generator
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401 - register all tables
from app.core.database import Base
from app.models.chat import ChatMessage, ChatSession
from app.schemas.history import ChatMessageResponse
from app.services import chat_context_service, chat_retrieval_service, history_service, rag_service
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
            captured["rewrite_prompt"] = "\n".join(str(message.content) for message in messages)
            return SimpleNamespace(content="When is the database assignment deadline?")

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
        }
    ]

    response = ChatMessageResponse.model_validate(rows[3])
    assert response.citations[0].title == "policy.pdf"


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
