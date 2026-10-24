"""Conversation memory and follow-up resolution (AI-63)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401 - register all tables
from app.core.database import Base
from app.models.chat import ChatMessage, ChatSession
from app.services import chat_context_service


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


def _session(
    db: Session,
    *,
    user_id: uuid.UUID | None = None,
    class_id: uuid.UUID | None = None,
) -> ChatSession:
    session = ChatSession(
        user_id=user_id or uuid.uuid4(),
        class_id=class_id or uuid.uuid4(),
        title="Context test",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _add_exchange(
    db: Session,
    session: ChatSession,
    *,
    question: str,
    answer: str,
    offset: int,
) -> None:
    created = datetime.now(UTC) + timedelta(seconds=offset)
    db.add_all(
        [
            ChatMessage(
                session_id=session.id,
                role="USER",
                content=question,
                created_at=created,
            ),
            ChatMessage(
                session_id=session.id,
                role="ASSISTANT",
                content=answer,
                created_at=created + timedelta(milliseconds=1),
            ),
        ]
    )
    db.commit()


def test_load_recent_exchanges_returns_last_complete_pairs(db_session: Session) -> None:
    session = _session(db_session)
    for index in range(1, 5):
        _add_exchange(
            db_session,
            session,
            question=f"question {index}",
            answer=f"answer {index}",
            offset=index,
        )

    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=session.user_id,
        class_id=session.class_id,
        max_exchanges=2,
    )

    assert [exchange.user for exchange in history] == ["question 3", "question 4"]
    assert [exchange.assistant for exchange in history] == ["answer 3", "answer 4"]


def test_load_recent_exchanges_drops_orphaned_messages(db_session: Session) -> None:
    session = _session(db_session)
    db_session.add(
        ChatMessage(
            session_id=session.id,
            role="ASSISTANT",
            content="orphaned answer",
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    _add_exchange(
        db_session,
        session,
        question="question 1",
        answer="answer 1",
        offset=1,
    )

    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=session.user_id,
        class_id=session.class_id,
    )

    assert len(history) == 1
    assert history[0].user == "question 1"


def test_load_recent_exchanges_enforces_ownership_and_class_scope(
    db_session: Session,
) -> None:
    session = _session(db_session)

    with pytest.raises(HTTPException) as forbidden:
        chat_context_service.load_recent_exchanges(
            db_session,
            session_id=session.id,
            user_id=uuid.uuid4(),
            class_id=session.class_id,
        )
    assert forbidden.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        chat_context_service.load_recent_exchanges(
            db_session,
            session_id=uuid.uuid4(),
            user_id=session.user_id,
            class_id=session.class_id,
        )
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as wrong_class:
        chat_context_service.load_recent_exchanges(
            db_session,
            session_id=session.id,
            user_id=session.user_id,
            class_id=uuid.uuid4(),
        )
    assert wrong_class.value.status_code == 404


def test_load_recent_exchanges_respects_character_budget(db_session: Session) -> None:
    session = _session(db_session)
    _add_exchange(
        db_session,
        session,
        question="old question " * 10,
        answer="old answer " * 10,
        offset=1,
    )
    _add_exchange(
        db_session,
        session,
        question="new question",
        answer="new answer",
        offset=2,
    )

    history = chat_context_service.load_recent_exchanges(
        db_session,
        session_id=session.id,
        user_id=session.user_id,
        class_id=session.class_id,
        max_chars=50,
    )

    assert [exchange.user for exchange in history] == ["new question"]


def test_history_context_drops_old_citation_markers() -> None:
    rendered = chat_context_service.format_history_context(
        [
            chat_context_service.ConversationExchange(
                user="What is the deadline?",
                assistant="The deadline is Friday [1][2].",
            )
        ]
    )

    assert "Friday" in rendered
    assert "[1]" not in rendered
    assert "[2]" not in rendered


def test_condense_question_uses_recent_history() -> None:
    captured: list[object] = []

    class FakeModel:
        async def ainvoke(self, messages):
            captured.extend(messages)
            return SimpleNamespace(content="When is the database assignment deadline?")

    history = [
        chat_context_service.ConversationExchange(
            user="What is the database assignment policy?",
            assistant="The policy describes the assignment deadline.",
        )
    ]

    rewritten = asyncio.run(
        chat_context_service.condense_question(
            history,
            "So when is the deadline?",
            model=FakeModel(),
        )
    )

    assert rewritten == "When is the database assignment deadline?"
    rendered = "\n".join(str(message.content) for message in captured)
    assert "database assignment policy" in rendered
    assert "So when is the deadline?" in rendered


def test_condense_question_falls_back_to_the_original_question() -> None:
    class BrokenModel:
        async def ainvoke(self, messages):
            raise RuntimeError("provider unavailable")

    history = [
        chat_context_service.ConversationExchange(
            user="What is the deadline?",
            assistant="Friday.",
        )
    ]

    rewritten = asyncio.run(
        chat_context_service.condense_question(
            history,
            "What about that one?",
            model=BrokenModel(),
        )
    )

    assert rewritten == "What about that one?"
