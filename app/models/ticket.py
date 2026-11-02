"""Student-created tickets and their replies (AI-81)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Ticket(Base):
    __tablename__ = "ticket"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN', 'ANSWERED', 'RESOLVED', 'EXPIRED')",
            name="ck_ticket_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    original_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="OPEN", server_default="OPEN"
    )
    # Starts a fresh expiration window when a resolved ticket is reopened.
    reopened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    replies: Mapped[list["TicketReply"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketReply.created_at",
    )


class TicketReply(Base):
    __tablename__ = "ticket_reply"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ticket.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    ticket: Mapped[Ticket] = relationship(back_populates="replies")
