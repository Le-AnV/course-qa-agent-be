"""History service — Chat session and message lifecycle management (AI-95, Epic E7)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.config.config import settings
from app.models.chat import ChatMessage, ChatSession

logger = logging.getLogger(__name__)

CONTENT_GAP_COLLECTION = "content_gap_queries"


def get_qdrant_client() -> Any | None:
    """Safely obtain a Qdrant client instance using application configuration."""
    try:
        from qdrant_client import QdrantClient

        return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not initialize QdrantClient in history service: %s", exc)
        return None


def ensure_content_gap_collection(client: Any) -> None:
    """Ensure the content_gap_queries collection exists in Qdrant (AC-01)."""
    try:
        from qdrant_client.http.models import Distance, VectorParams
        collection_name = CONTENT_GAP_COLLECTION
        if not client.collection_exists(collection_name):
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=settings.vector_size, distance=Distance.COSINE),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to ensure collection 'content_gap_queries' in Qdrant: %s", exc)


def sync_content_gap_query(
    message_id: str,
    class_id: str,
    student_id: str,
    embedding: list[float],
    *,
    source: str = "web",
    fallback_reason: str | None = None,
    concept_tag: str | None = None,
    embedding_model_version: str | None = None,
    qdrant_client: Any | None = None,
) -> None:
    """Upsert question query embedding into Qdrant collection 'content_gap_queries' (AC-01, AC-03).

    Zero extra API calls: reuses the embedding vector already computed during retrieval.
    Strictly avoids storing question text (privacy safeguard).
    """
    if not embedding:
        return

    client = qdrant_client or get_qdrant_client()
    if client is None:
        return

    try:
        from qdrant_client.http.models import PointStruct

        ensure_content_gap_collection(client)

        point = PointStruct(
            id=message_id,
            vector=embedding,
            payload={
                "message_id": str(message_id),
                "class_id": str(class_id),
                "student_id": str(student_id),
                "timestamp": datetime.now(UTC).isoformat(),
                "source": str(source),
                "fallback_reason": fallback_reason,
                "concept_tag": concept_tag or "",
                "embedding_model_version": embedding_model_version or settings.embedding_model,
            },
        )
        client.upsert(collection_name=CONTENT_GAP_COLLECTION, points=[point])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to sync content gap query to Qdrant: %s", exc)


def delete_content_gap_queries(
    message_ids: list[str],
    qdrant_client: Any | None = None,
) -> None:
    """Delete points from Qdrant collection 'content_gap_queries' by message ID (AC-05)."""
    if not message_ids:
        return

    client = qdrant_client or get_qdrant_client()
    if client is None:
        return

    try:
        from qdrant_client.http.models import PointIdsList

        client.delete(
            collection_name=CONTENT_GAP_COLLECTION,
            points_selector=PointIdsList(points=message_ids),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to delete points from Qdrant content_gap_queries: %s", exc)


def get_sessions_by_class(
    db: Session,
    user_id: uuid.UUID,
    class_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ChatSession], int]:
    """Retrieve chat sessions belonging to a user, optionally filtered by class.

    Ordered by updated_at DESC to display the newest conversations first (AC-04, AC-06).
    """
    query = select(ChatSession).where(ChatSession.user_id == user_id)
    if class_id is not None:
        query = query.where(ChatSession.class_id == class_id)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = db.scalar(count_query) or 0

    # Sort pinned first, then newest first and paginate
    sessions = (
        db.scalars(
            query.order_by(
                ChatSession.is_pinned.desc(),
                ChatSession.pinned_at.asc().nullslast(),
                ChatSession.updated_at.desc(),
                ChatSession.created_at.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
    )

    return list(sessions), total


def get_session_messages(
    db: Session,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ChatSession:
    """Load session and all contained messages in chronological order (created_at ASC) (AC-05, AC-07).

    Enforces row-level ownership: raises 403 Forbidden if the session belongs to another user.
    """
    session = db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat session not found.",
        )

    if session.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: you do not own this chat session.",
        )

    # Ensure messages are ordered chronologically
    return session


def create_session(
    db: Session,
    user_id: uuid.UUID,
    class_id: uuid.UUID,
    title: str | None = None,
) -> ChatSession:
    """Create a new chat session (AC-03)."""
    session = ChatSession(
        user_id=user_id,
        class_id=class_id,
        title=title,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _citation_dicts(citations: Sequence[Any] | None) -> list[dict[str, Any]]:
    """Convert Pydantic models or mappings into JSON-serializable citation dicts."""

    if not citations:
        return []

    normalized: list[dict[str, Any]] = []
    for citation in citations:
        if hasattr(citation, "model_dump"):
            normalized.append(citation.model_dump())
        elif isinstance(citation, dict):
            normalized.append(dict(citation))
        else:
            normalized.append(dict(citation))
    return normalized


def rename_session(
    db: Session,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
) -> ChatSession:
    """Rename an owned chat session without changing its recent-activity order.

    ``updated_at`` drives the sidebar ordering, so a rename writes the title
    only; the column is explicitly set to itself to bypass the model onupdate.
    """

    session = db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat session not found.",
        )

    if session.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: you do not own this chat session.",
        )

    db.execute(
        update(ChatSession)
        .where(ChatSession.id == session.id)
        .values(title=title.strip(), updated_at=ChatSession.updated_at)
    )
    db.commit()
    db.refresh(session)
    return session


def toggle_pin_session(
    db: Session,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    is_pinned: bool | None = None,
) -> ChatSession:
    """Pin or unpin an owned chat session.

    If is_pinned is None, toggles current status.
    Setting to pinned records pinned_at timestamp; unpinning clears it.
    """
    session = db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat session not found.",
        )

    if session.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: you do not own this chat session.",
        )

    new_pin_state = not session.is_pinned if is_pinned is None else is_pinned
    session.is_pinned = new_pin_state
    session.pinned_at = datetime.now(UTC) if new_pin_state else None

    db.commit()
    db.refresh(session)
    return session


def append_message(
    db: Session,
    session_id: uuid.UUID,
    role: str,
    content: str,
    used_web_search: bool = False,
    citations: Sequence[Any] | None = None,
    suggested_questions: Sequence[str] | None = None,
    excluded_from_context: bool = False,
    thinking_ms: int | None = None,
    trace: Sequence[str] | None = None,
) -> ChatMessage:
    """Append a single message to an existing session and update session's updated_at timestamp."""
    message = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        used_web_search=used_web_search,
        citations=_citation_dicts(citations),
        suggested_questions=list(suggested_questions) if suggested_questions else None,
        excluded_from_context=excluded_from_context,
        thinking_ms=thinking_ms,
        trace=list(trace) if trace else None,
    )
    db.add(message)

    # Touch session updated_at
    session = db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if session is not None:
        session.updated_at = func.now()

    db.commit()
    db.refresh(message)
    return message


def update_suggested_questions(
    db: Session,
    message_id: uuid.UUID,
    questions: Sequence[str],
) -> ChatMessage | None:
    """Attach generated follow-up questions to an already persisted answer."""

    message = db.get(ChatMessage, message_id)
    if message is None:
        return None
    message.suggested_questions = list(questions) if questions else None
    db.commit()
    db.refresh(message)
    return message


def save_chat_turn(
    db: Session,
    user_id: uuid.UUID,
    class_id: uuid.UUID,
    user_content: str,
    assistant_content: str,
    session_id: uuid.UUID | None = None,
    used_web_search: bool = False,
    citations: Sequence[Any] | None = None,
    suggested_questions: Sequence[str] | None = None,
    thinking_ms: int | None = None,
    trace: Sequence[str] | None = None,
    query_embedding: list[float] | None = None,
    fallback_reason: str | None = None,
    concept_tag: str | None = None,
    qdrant_client: Any | None = None,
    excluded_from_context: bool = False,
) -> tuple[ChatSession, ChatMessage, ChatMessage]:
    """Persist a complete Q&A turn (User prompt + Assistant response) into the database (AC-01, AC-02, AC-03).

    1. If session_id is None, initializes a new session with title = first 50 chars of user query (AC-03).
    2. Persists USER message and ASSISTANT message (with used_web_search flag) (AC-02).
    3. Stores the assistant citations so history reloads keep the original answer traceable (AI-63 AC-03).
    4. If used_web_search is True and query_embedding is provided, syncs to Qdrant content_gap_queries (AC-03).
       NOTE: Strictly does NOT invoke Gemini Embedding API — reuses existing query_embedding.
    5. ``excluded_from_context`` keeps a refused turn out of later prompts while
       leaving it visible when the conversation is reopened.
    """
    if session_id is None:
        # Title defaults to the first 50 characters of the initial question
        raw_title = user_content.strip()
        title = raw_title[:50] if len(raw_title) > 50 else raw_title
        session = ChatSession(
            user_id=user_id,
            class_id=class_id,
            title=title,
        )
        db.add(session)
        db.flush()
    else:
        session = db.scalar(select(ChatSession).where(ChatSession.id == session_id))
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat session not found.",
            )
        if session.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access forbidden: you do not own this chat session.",
            )

    # 2. Insert both USER and ASSISTANT messages. Explicit, separated
    # timestamps keep the pair ordered even when a database stores both rows
    # in one statement with microsecond-level clock resolution.
    turn_started = datetime.now(UTC)
    user_msg = ChatMessage(
        session_id=session.id,
        role="USER",
        content=user_content,
        used_web_search=False,
        created_at=turn_started,
        excluded_from_context=excluded_from_context,
    )
    assistant_msg = ChatMessage(
        session_id=session.id,
        role="ASSISTANT",
        content=assistant_content,
        used_web_search=used_web_search,
        citations=_citation_dicts(citations),
        suggested_questions=list(suggested_questions) if suggested_questions else None,
        thinking_ms=thinking_ms,
        trace=list(trace) if trace else None,
        created_at=turn_started + timedelta(milliseconds=1),
        excluded_from_context=excluded_from_context,
    )

    db.add(user_msg)
    db.flush()
    assistant_msg.parent_message_id = user_msg.id
    db.add(assistant_msg)

    # Update session updated_at
    session.updated_at = func.now()
    db.commit()

    db.refresh(session)
    db.refresh(user_msg)
    db.refresh(assistant_msg)

    # 3. Synchronize with Qdrant content_gap_queries (AC-01: record every query)
    if query_embedding:
        sync_content_gap_query(
            message_id=str(assistant_msg.id),
            class_id=str(class_id),
            student_id=str(user_id),
            embedding=query_embedding,
            source="web" if used_web_search else "material",
            fallback_reason=fallback_reason if used_web_search else None,
            concept_tag=concept_tag,
            embedding_model_version=settings.embedding_model,
            qdrant_client=qdrant_client,
        )

    return session, user_msg, assistant_msg


def delete_session(
    db: Session,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    qdrant_client: Any | None = None,
) -> bool:
    """Delete a chat session and cascade deletion to messages and vector points (AC-05, AI-99).

    Order of operations:
    1. Check user ownership (403 if unauthorized, 404 if missing).
    2. Collect message IDs with used_web_search=True and delete from Qdrant first.
    3. Delete chat_session from PostgreSQL (cascades to chat_message).
    """
    session = db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat session not found.",
        )

    if session.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: you do not own this chat session.",
        )

    # 1. Identify Qdrant vector points to delete
    gap_messages = (
        db.scalars(
            select(ChatMessage.id).where(
                ChatMessage.session_id == session.id,
                ChatMessage.used_web_search.is_(True),
            )
        ).all()
    )
    if gap_messages:
        delete_content_gap_queries(
            message_ids=[str(msg_id) for msg_id in gap_messages],
            qdrant_client=qdrant_client,
        )

    # 2. Delete from DB (cascade removes all chat_message rows)
    db.delete(session)
    db.commit()
    return True


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards để user input được match literal."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _extract_snippet(content: str, keyword: str, context_chars: int = 100) -> str:
    """Trích xuất đoạn văn bản xung quanh từ khóa để hiển thị làm preview."""
    idx = content.lower().find(keyword.lower())
    if idx == -1:
        return content[:context_chars] + ("..." if len(content) > context_chars else "")
    start = max(0, idx - context_chars // 2)
    end = min(len(content), idx + len(keyword) + context_chars // 2)
    snippet = content[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    return snippet


def search_sessions(
    db: Session,
    user_id: uuid.UUID,
    query: str,
    class_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """
    Tìm kiếm phiên chat theo từ khóa trong title và/hoặc message content.

    Chiến lược:
    1. Title match → match_in='title', snippet = title (UI ẩn snippet nếu trùng title).
    2. Message match → match_in='message', snippet từ message đầu tiên khớp.
    3. Nếu 1 session khớp cả 2 → ưu tiên match_in='title'.
    4. Sort: updated_at DESC, created_at DESC.
    5. RLS: WHERE ChatSession.user_id == user_id.
    6. Pagination + total count bằng SQL.
    """
    query = query.strip()
    if not query:
        return [], 0

    keyword = f"%{_escape_like(query)}%"

    message_match = exists().where(
        ChatMessage.session_id == ChatSession.id,
        ChatMessage.content.ilike(keyword, escape="\\"),
    )

    base = select(ChatSession).where(
        ChatSession.user_id == user_id,
        or_(
            ChatSession.title.ilike(keyword, escape="\\"),
            message_match,
        ),
    )
    if class_id is not None:
        base = base.where(ChatSession.class_id == class_id)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    sessions = db.scalars(
        base.order_by(ChatSession.updated_at.desc(), ChatSession.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()

    if not sessions:
        return [], total

    session_ids = [session.id for session in sessions]
    matching_messages = db.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.session_id.in_(session_ids),
            ChatMessage.content.ilike(keyword, escape="\\"),
        )
        .order_by(ChatMessage.session_id, ChatMessage.created_at.asc())
    ).all()

    first_message_by_session: dict[uuid.UUID, ChatMessage] = {}
    for message in matching_messages:
        first_message_by_session.setdefault(message.session_id, message)

    results: list[dict] = []
    for session in sessions:
        title_matches = bool(session.title and query.lower() in session.title.lower())
        if title_matches:
            results.append({
                "session_id": session.id,
                "title": session.title,
                "updated_at": session.updated_at,
                "snippet": session.title or "",
                "match_in": "title",
            })
            continue

        message = first_message_by_session.get(session.id)
        results.append({
            "session_id": session.id,
            "title": session.title,
            "updated_at": session.updated_at,
            "snippet": _extract_snippet(message.content, query) if message else "",
            "match_in": "message",
        })

    return results, total
