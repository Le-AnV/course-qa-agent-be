"""Class-scoped RAG chat endpoint with Server-Sent Events (AI-61)."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.course import Class, Enrollment
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services import rag_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _require_class_access(db: Session, user: User, class_id) -> None:
    clazz = db.query(Class).filter(Class.id == class_id).first()
    if clazz is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")

    role_name = getattr(getattr(user, "role", None), "name", "").upper()
    if role_name == "ADMIN":
        return
    if role_name == "TRAINER" and clazz.trainer_id == user.id:
        return
    if role_name in {"STUDENT", "TEACHING_ASSISTANT"}:
        enrollment = (
            db.query(Enrollment)
            .filter(
                Enrollment.class_id == class_id,
                Enrollment.student_id == user.id,
                Enrollment.status == "ACTIVE",
            )
            .first()
        )
        if enrollment is not None:
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this class.",
    )


def _sse_event(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("", response_class=StreamingResponse)
async def chat(
    payload: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Retrieve class materials and stream a grounded answer as SSE."""
    _require_class_access(db, user, payload.class_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.chat_timeout_seconds
    try:
        documents = await asyncio.wait_for(
            run_in_threadpool(
                rag_service.retrieve_documents, payload.message, str(payload.class_id)
            ),
            timeout=settings.chat_timeout_seconds,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="The request timed out. Please try again.",
        ) from None
    except Exception as exc:  # noqa: BLE001 - integration failure must become a finite API error
        logger.warning("Course material retrieval failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Course materials are temporarily unavailable.",
        ) from None

    async def events() -> AsyncIterator[str]:
        try:
            remaining = max(0.0, deadline - loop.time())
            async with asyncio.timeout(remaining):
                async for text in rag_service.stream_answer(payload.message, documents):
                    if text:
                        yield _sse_event("token", {"text": text})
                for citation in rag_service.build_citations(documents):
                    yield _sse_event("citation", citation)
        except TimeoutError:
            yield _sse_event("error", {"message": "The request timed out. Please try again."})
            return
        except Exception as exc:  # noqa: BLE001 - provider failures are reported through SSE
            logger.warning("Gemini answer stream failed: %s", exc)
            yield _sse_event("error", {"message": "The answer could not be generated."})
            return
        yield _sse_event("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
