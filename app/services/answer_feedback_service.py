"""Ownership and validity checks shared by feedback and confirmed tickets."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage, ChatSession
from app.models.ticket import Ticket
from app.models.user import User
from app.services.ticket_service import create_student_ticket


def owned_answer(db: Session, user: User, message_id: uuid.UUID) -> ChatMessage:
    """Lock a completed answer owned by the student, hiding foreign messages."""
    if user.role.name != "STUDENT":
        raise HTTPException(403, "Only students can give answer feedback.")
    message = db.scalar(
        select(ChatMessage).join(ChatSession)
        .where(ChatMessage.id == message_id, ChatSession.user_id == user.id)
        .with_for_update(of=ChatMessage).execution_options(populate_existing=True)
    )
    if message is None:
        raise HTTPException(404, "Message not found.")
    if message.role != "ASSISTANT" or not message.content.strip():
        raise HTTPException(409, "Feedback requires a completed assistant answer.")
    return message


def create_answer_ticket(db: Session, user: User, message_id: uuid.UUID, details: str | None) -> tuple[Ticket, bool]:
    """Snapshot a negatively rated answer into one ticket.

    Return the ticket and whether this call created it; confirmed retries reuse
    the existing ticket even if feedback or enrollment changed afterward.
    """
    answer = owned_answer(db, user, message_id)
    existing = db.scalar(select(Ticket).where(Ticket.source_message_id == message_id))
    if existing is not None:
        return existing, False
    if answer.feedback != "not_helpful":
        raise HTTPException(409, "Save Not helpful feedback before creating a ticket.")
    question = db.get(ChatMessage, answer.parent_message_id) if answer.parent_message_id else None
    if question is None or question.role != "USER" or question.session_id != answer.session_id:
        raise HTTPException(409, "The original question could not be identified.")
    try:
        ticket = create_student_ticket(
            db, student=user, class_id=answer.session.class_id,
            question=question.content, original_question=question.content,
            agent_answer=answer.content, citations=answer.citations or [],
            source_message_id=message_id, details=details,
        )
    except IntegrityError as error:
        db.rollback()
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint not in {"uq_ticket_source_message_id", "ticket_source_message_id_key"}:
            raise
        ticket = db.scalar(select(Ticket).where(Ticket.source_message_id == message_id))
        if ticket is None:
            raise
        return ticket, False
    if ticket is None:
        raise HTTPException(403, "An active class and enrollment are required.")
    return ticket, True
