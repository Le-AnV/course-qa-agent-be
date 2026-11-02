"""Student-initiated ticket operations (AI-81)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.course import Class, Enrollment
from app.models.ticket import Ticket, TicketReply
from app.models.user import User
from app.schemas.tickets import TicketInboxItem, TicketStatus
from app.services.course_service import class_ids_for_trainer
from app.services.user_service import role_name_of

TICKET_EXPIRATION = timedelta(days=3)


def expire_open_tickets(db: Session, *, now: datetime | None = None) -> int:
    """Expire unanswered tickets after three days from creation."""
    cutoff = (now or datetime.now(UTC)) - TICKET_EXPIRATION
    result = db.execute(
        update(Ticket)
        .where(Ticket.status == "OPEN", Ticket.created_at <= cutoff)
        .values(status="EXPIRED")
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.expire_all()
    return result.rowcount or 0


def create_student_ticket(
    db: Session,
    *,
    student: User,
    class_id: uuid.UUID,
    question: str,
    original_question: str | None = None,
    agent_answer: str | None = None,
    details: str | None = None,
) -> Ticket | None:
    """Create a ticket only when the caller is enrolled in the selected class."""

    if role_name_of(student) != "STUDENT":
        return None

    clazz = db.query(Class).filter(Class.id == class_id).first()
    if clazz is None or clazz.status != "ACTIVE":
        return None

    enrollment = (
        db.query(Enrollment)
        .filter(
            Enrollment.class_id == class_id,
            Enrollment.student_id == student.id,
            Enrollment.status == "ACTIVE",
        )
        .first()
    )
    if enrollment is None:
        return None

    ticket = Ticket(
        class_id=class_id,
        student_id=student.id,
        question=question,
        original_question=original_question,
        agent_answer=agent_answer,
    )
    db.add(ticket)
    db.flush()
    if details:
        db.add(TicketReply(ticket_id=ticket.id, user_id=student.id, message=details))
    db.commit()
    db.refresh(ticket)
    return ticket


def get_student_ticket(db: Session, *, ticket_id: uuid.UUID, student_id: uuid.UUID) -> Ticket | None:
    expire_open_tickets(db)
    return (
        db.query(Ticket)
        .filter(Ticket.id == ticket_id, Ticket.student_id == student_id)
        .first()
    )


def add_student_reply(
    db: Session,
    *,
    ticket_id: uuid.UUID,
    student_id: uuid.UUID,
    message: str,
) -> TicketReply | None:
    ticket = get_student_ticket(db, ticket_id=ticket_id, student_id=student_id)
    if ticket is None or ticket.status != "OPEN":
        return None

    reply = TicketReply(ticket_id=ticket.id, user_id=student_id, message=message)
    db.add(reply)
    db.commit()
    db.refresh(reply)
    return reply


def add_trainer_reply(
    db: Session,
    *,
    ticket_id: uuid.UUID,
    trainer_id: uuid.UUID,
    message: str,
) -> TicketReply | None:
    expire_open_tickets(db)
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None or ticket.status != "OPEN":
        return None
    if ticket.class_id not in class_ids_for_trainer(db, trainer_id):
        raise PermissionError("You are not assigned to this class.")

    reply = TicketReply(ticket_id=ticket.id, user_id=trainer_id, message=message)
    db.add(reply)
    ticket.status = "ANSWERED"
    db.commit()
    db.refresh(reply)
    return reply


def resolve_student_ticket(
    db: Session, *, ticket_id: uuid.UUID, student_id: uuid.UUID
) -> Ticket | None:
    ticket = get_student_ticket(db, ticket_id=ticket_id, student_id=student_id)
    if ticket is None or ticket.status != "ANSWERED":
        return None
    ticket.status = "RESOLVED"
    db.commit()
    db.refresh(ticket)
    return ticket


def list_trainer_tickets(
    db: Session,
    *,
    trainer_id: uuid.UUID,
    class_id: uuid.UUID | None = None,
    status: TicketStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[TicketInboxItem], int]:
    expire_open_tickets(db)
    assigned = class_ids_for_trainer(db, trainer_id)
    if class_id is not None and class_id not in assigned:
        raise PermissionError("You are not assigned to this class.")
    scope = {class_id} if class_id is not None else assigned
    query = (
        db.query(Ticket, User.full_name, Class.name, Class.code)
        .join(User, Ticket.student_id == User.id)
        .join(Class, Ticket.class_id == Class.id)
        .filter(Ticket.class_id.in_(scope))
    )
    if status is not None:
        query = query.filter(Ticket.status == status)
    total = query.count()
    rows = (
        query.order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        TicketInboxItem(
            id=ticket.id, class_id=ticket.class_id, student_id=ticket.student_id,
            question=ticket.question, original_question=ticket.original_question,
            status=ticket.status, created_at=ticket.created_at,
            agent_answer=ticket.agent_answer,
            student_name=student_name, class_name=class_name, class_code=class_code,
            replies=ticket.replies,
        )
        for ticket, student_name, class_name, class_code in rows
    ]
    return items, total
