"""Student-created support tickets (AI-81)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.tickets import (
    TicketCreate,
    TicketListResponse,
    TicketRead,
    TicketReplyCreate,
    TicketReplyRead,
    TicketStatus,
)
from app.services import ticket_service
from app.services.user_service import role_name_of

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


def _require_student(user: User) -> None:
    if role_name_of(user) != "STUDENT":
        raise HTTPException(status_code=403, detail="Only students can create or update tickets.")


@router.get("", response_model=TicketListResponse)
def list_tickets(
    class_id: uuid.UUID | None = None,
    status: TicketStatus | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TicketListResponse:
    if role_name_of(user) != "TRAINER":
        raise HTTPException(status_code=403, detail="Only trainers can view the ticket inbox.")
    try:
        items, total = ticket_service.list_trainer_tickets(
            db, trainer_id=user.id, class_id=class_id, status=status,
            page=page, page_size=page_size,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return TicketListResponse(items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=TicketRead, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TicketRead:
    _require_student(user)
    ticket = ticket_service.create_student_ticket(
        db,
        student=user,
        class_id=payload.class_id,
        question=payload.question,
        agent_answer=payload.agent_answer,
        details=payload.details,
    )
    if ticket is None:
        raise HTTPException(status_code=403, detail="You are not an active member of this class.")
    return TicketRead.model_validate(ticket)


@router.post("/{ticket_id}/replies", response_model=TicketReplyRead, status_code=201)
def add_ticket_reply(
    ticket_id: uuid.UUID,
    payload: TicketReplyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TicketReplyRead:
    _require_student(user)
    reply = ticket_service.add_student_reply(
        db,
        ticket_id=ticket_id,
        student_id=user.id,
        message=payload.message,
    )
    if reply is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return TicketReplyRead.model_validate(reply)
