"""Student-initiated ticket operations (AI-81, AI-89)."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models.course import Class, ClassTrainer, Enrollment
from app.models.system_config import SystemConfig
from app.models.ticket import Ticket, TicketReply
from app.models.user import User
from app.schemas.tickets import TicketClassCounts, TicketInboxItem, TicketStatus
from app.services import email_service
from app.services.course_service import class_ids_for_trainer
from app.services.holiday_service import business_elapsed, holiday_dates, local_date
from app.services.user_service import role_name_of

logger = logging.getLogger(__name__)

TICKET_EXPIRATION = timedelta(days=7)
TICKET_ESCALATION_SLA_KEY = "ticket_escalation_sla_hours"
DEFAULT_TICKET_ESCALATION_SLA_HOURS = 24
MAX_TICKET_ESCALATION_SLA_HOURS = int(TICKET_EXPIRATION.total_seconds() // 3600) - 1
RESOLVABLE_STATUSES = ("OPEN", "ANSWERED")


def expire_open_tickets(db: Session, *, now: datetime | None = None) -> int:
    """Expire unanswered tickets after seven days from creation."""
    cutoff = (now or datetime.now(UTC)) - TICKET_EXPIRATION
    result = db.execute(
        update(Ticket)
        .where(
            Ticket.status == "OPEN",
            func.coalesce(Ticket.reopened_at, Ticket.created_at) <= cutoff,
        )
        .values(status="EXPIRED")
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.expire_all()
    return result.rowcount or 0


def ticket_escalation_sla(db: Session) -> timedelta:
    """SLA before an open ticket goes to the backup trainer (default 24h).

    Capped below TICKET_EXPIRATION so the ticket is still open when escalated.
    """
    row = db.get(SystemConfig, TICKET_ESCALATION_SLA_KEY)
    hours = DEFAULT_TICKET_ESCALATION_SLA_HOURS
    if row is not None:
        try:
            hours = int(row.value)
        except (TypeError, ValueError):
            pass
    return timedelta(hours=max(1, min(hours, MAX_TICKET_ESCALATION_SLA_HOURS)))


def _backup_trainer_id(db: Session, class_id: uuid.UUID) -> uuid.UUID | None:
    clazz = db.query(Class).filter(Class.id == class_id).first()
    if clazz is None:
        return None
    row = (
        db.query(ClassTrainer.trainer_id)
        .filter(
            ClassTrainer.class_id == class_id,
            ClassTrainer.trainer_id != clazz.trainer_id,
        )
        .order_by(ClassTrainer.created_at.asc(), ClassTrainer.id.asc())
        .first()
    )
    return row[0] if row is not None else None


def escalate_stale_tickets(
    db: Session,
    *,
    now: datetime | None = None,
    class_ids: set[uuid.UUID] | None = None,
) -> int:
    """Escalate each open ticket past the holiday-aware SLA once.

    The SQL cutoff is the wall-clock lower bound (a ticket that is not old
    enough in wall-clock terms cannot be stale in business time either). The
    final decision removes full Vietnam holiday days from the elapsed window,
    so holidays do not consume the SLA (AI-156 AC-04).

    ``class_ids`` limits the sweep to the classes the caller can already see, so
    reading one ticket never walks every stale ticket in the database.
    """
    if class_ids is not None and not class_ids:
        return 0
    moment = now or datetime.now(UTC)
    sla = ticket_escalation_sla(db)
    cutoff = moment - sla
    query = db.query(Ticket).filter(
        Ticket.status == "OPEN",
        func.coalesce(Ticket.reopened_at, Ticket.created_at) <= cutoff,
        Ticket.escalated_at.is_(None),
    )
    if class_ids is not None:
        query = query.filter(Ticket.class_id.in_(class_ids))
    stale = query.all()
    escalated = 0
    for ticket in stale:
        started = ticket.reopened_at or ticket.created_at
        holidays = holiday_dates(
            db,
            local_date(started),
            local_date(moment),
            ticket.class_id,
        )
        if business_elapsed(started, moment, holidays) < sla:
            continue
        backup_id = _backup_trainer_id(db, ticket.class_id)
        if backup_id is None:
            continue
        ticket.escalated_to_id = backup_id
        ticket.escalated_at = moment
        escalated += 1
    if escalated:
        db.commit()
        db.expire_all()
    return escalated


def create_student_ticket(
    db: Session,
    *,
    student: User,
    class_id: uuid.UUID,
    question: str,
    original_question: str | None = None,
    agent_answer: str | None = None,
    details: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    source_message_id: uuid.UUID | None = None,
) -> Ticket | None:
    """Create a ticket for an active student and class, preserving supplied snapshots."""

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
        citations=citations or [],
        source_message_id=source_message_id,
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
    ticket = (
        db.query(Ticket)
        .join(Class, Ticket.class_id == Class.id)
        .filter(
            Ticket.id == ticket_id,
            Ticket.student_id == student_id,
            Class.status != "ARCHIVED",
        )
        .first()
    )
    if ticket is not None and escalate_stale_tickets(db, class_ids={ticket.class_id}):
        db.refresh(ticket)
    return ticket


def add_student_reply(
    db: Session,
    *,
    ticket_id: uuid.UUID,
    student_id: uuid.UUID,
    message: str,
) -> TicketReply | None:
    ticket = get_student_ticket(db, ticket_id=ticket_id, student_id=student_id)
    if ticket is None or ticket.status not in ("OPEN", "ANSWERED"):
        return None

    reply = TicketReply(ticket_id=ticket.id, user_id=student_id, message=message)
    db.add(reply)
    if ticket.status == "ANSWERED":
        # A follow-up means the student still needs help, so the ticket goes
        # back to the trainer queue with a fresh expiration window.
        ticket.status = "OPEN"
        ticket.reopened_at = datetime.now(UTC)
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
    if ticket.class_id not in class_ids_for_trainer(db, trainer_id, active_only=True):
        raise PermissionError("You are not assigned to this class.")
    if escalate_stale_tickets(db, class_ids={ticket.class_id}):
        db.refresh(ticket)

    reply = TicketReply(ticket_id=ticket.id, user_id=trainer_id, message=message)
    db.add(reply)
    ticket.status = "ANSWERED"
    db.commit()
    db.refresh(reply)
    _queue_student_reply_email(db, ticket=ticket, trainer_id=trainer_id, reply=reply)
    return reply


def _queue_student_reply_email(
    db: Session, *, ticket: Ticket, trainer_id: uuid.UUID, reply: TicketReply
) -> None:
    """AC-03: tell the student their trainer answered; never fail the saved reply."""
    student = db.query(User).filter(User.id == ticket.student_id).first()
    trainer = db.query(User).filter(User.id == trainer_id).first()
    if student is None or trainer is None:
        return
    clazz = db.query(Class).filter(Class.id == ticket.class_id).first()
    subject, body = email_service.ticket_reply_email_body(
        student.full_name,
        trainer.full_name,
        clazz.code if clazz is not None else None,
        ticket.question,
        reply.message,
    )
    try:
        email_service.queue_email(db, student.email, subject, body, user_id=student.id)
    except Exception:
        logger.exception("Failed to queue the ticket reply email for ticket %s", ticket.id)


def get_trainer_ticket(
    db: Session, *, ticket_id: uuid.UUID, trainer_id: uuid.UUID
) -> Ticket | None:
    """Return a ticket only when it belongs to one of the trainer's classes."""
    expire_open_tickets(db)
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        return None
    if ticket.class_id not in class_ids_for_trainer(db, trainer_id, active_only=True):
        raise PermissionError("You are not assigned to this class.")
    return ticket


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


def resolve_trainer_ticket(
    db: Session, *, ticket_id: uuid.UUID, trainer_id: uuid.UUID
) -> Ticket | None:
    """AC-01: the assigned trainer marks an open or answered ticket as resolved."""
    ticket = get_trainer_ticket(db, ticket_id=ticket_id, trainer_id=trainer_id)
    if ticket is None or ticket.status not in RESOLVABLE_STATUSES:
        return None
    ticket.status = "RESOLVED"
    db.commit()
    db.refresh(ticket)
    return ticket


def _reopened_status(db: Session, ticket: Ticket) -> str:
    """Return ANSWERED when a trainer already replied, otherwise OPEN."""
    has_trainer_reply = (
        db.query(TicketReply.id)
        .filter(
            TicketReply.ticket_id == ticket.id,
            TicketReply.user_id != ticket.student_id,
        )
        .first()
        is not None
    )
    return "ANSWERED" if has_trainer_reply else "OPEN"


def reopen_ticket(db: Session, *, ticket_id: uuid.UUID) -> Ticket | None:
    """AC-02: a resolved ticket goes back to Answered (or Open without a reply)."""
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None or ticket.status != "RESOLVED":
        return None
    ticket.status = _reopened_status(db, ticket)
    ticket.reopened_at = datetime.now(UTC)
    db.commit()
    db.refresh(ticket)
    return ticket


def ticket_counts_for_trainer(
    db: Session,
    *,
    trainer_id: uuid.UUID,
    class_id: uuid.UUID | None = None,
) -> list[TicketClassCounts]:
    """AC-03: open/completed ticket totals for each class the trainer teaches."""
    expire_open_tickets(db)
    assigned = class_ids_for_trainer(db, trainer_id, active_only=True)
    if class_id is not None and class_id not in assigned:
        raise PermissionError("You are not assigned to this class.")
    scope = {class_id} if class_id is not None else assigned
    if not scope:
        return []

    classes = (
        db.query(Class)
        .filter(Class.id.in_(scope), Class.status != "ARCHIVED")
        .order_by(Class.code)
        .all()
    )
    active_ids = [clazz.id for clazz in classes]
    rows = (
        db.query(Ticket.class_id, Ticket.status, func.count(Ticket.id))
        .filter(Ticket.class_id.in_(active_ids))
        .group_by(Ticket.class_id, Ticket.status)
        .all()
    )
    by_status: dict[uuid.UUID, dict[str, int]] = {}
    for ticket_class_id, status, count in rows:
        by_status.setdefault(ticket_class_id, {})[status] = count

    counts: list[TicketClassCounts] = []
    for clazz in classes:
        statuses = by_status.get(clazz.id, {})
        counts.append(
            TicketClassCounts(
                class_id=clazz.id,
                class_code=clazz.code,
                class_name=clazz.name,
                class_status=clazz.status or "ACTIVE",
                open_count=statuses.get("OPEN", 0) + statuses.get("ANSWERED", 0),
                completed_count=statuses.get("RESOLVED", 0),
                expired_count=statuses.get("EXPIRED", 0),
            )
        )
    return counts


def _inbox_items(rows) -> list[TicketInboxItem]:
    return [
        TicketInboxItem(
            id=ticket.id, class_id=ticket.class_id, student_id=ticket.student_id,
            question=ticket.question, original_question=ticket.original_question,
            status=ticket.status, created_at=ticket.created_at,
            agent_answer=ticket.agent_answer,
            citations=ticket.citations, source_message_id=ticket.source_message_id,
            escalated_to_id=ticket.escalated_to_id, escalated_at=ticket.escalated_at,
            escalated_to_name=ticket.escalated_to_name,
            student_name=student_name, class_name=class_name, class_code=class_code,
            class_status=class_status or "ACTIVE",
            replies=ticket.replies,
        )
        for ticket, student_name, class_name, class_code, class_status in rows
    ]


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
    assigned = class_ids_for_trainer(db, trainer_id, active_only=True)
    if class_id is not None and class_id not in assigned:
        raise PermissionError("You are not assigned to this class.")
    scope = {class_id} if class_id is not None else assigned
    escalate_stale_tickets(db, class_ids=scope)
    query = (
        db.query(Ticket, User.full_name, Class.name, Class.code, Class.status)
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
    return _inbox_items(rows), total


def list_student_tickets(
    db: Session,
    *,
    student_id: uuid.UUID,
    class_id: uuid.UUID | None = None,
    status: TicketStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[TicketInboxItem], int]:
    """List only the caller's own tickets for the student "My Tickets" page."""
    expire_open_tickets(db)
    query = (
        db.query(Ticket, User.full_name, Class.name, Class.code, Class.status)
        .join(User, Ticket.student_id == User.id)
        .join(Class, Ticket.class_id == Class.id)
        .filter(Ticket.student_id == student_id, Class.status != "ARCHIVED")
    )
    if class_id is not None:
        query = query.filter(Ticket.class_id == class_id)
    if status is not None:
        query = query.filter(Ticket.status == status)
    total = query.count()
    rows = (
        query.order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return _inbox_items(rows), total
