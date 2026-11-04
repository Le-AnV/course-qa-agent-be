"""SQLAlchemy models for ChatSession and ChatMessage (AI-95, Epic E7)."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.course import Class
    from app.models.ticket import Ticket
    from app.models.user import User


class ChatSession(Base):
    """Represents a conversation session between a user and the AI assistant within a class."""

    __tablename__ = "chat_session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User")
    clazz: Mapped["Class"] = relationship("Class")
    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    """Represents an individual message in a chat session (User prompt or Assistant reply)."""

    __tablename__ = "chat_message"
    __table_args__ = (
        CheckConstraint("feedback IN ('helpful', 'not_helpful')", name="ck_chat_message_feedback"),
        CheckConstraint(
            "role IN ('USER', 'ASSISTANT', 'SYSTEM')",
            name="ck_chat_message_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    feedback: Mapped[str | None] = mapped_column(String(20), nullable=True)
    parent_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("chat_message.id", ondelete="SET NULL"), nullable=True
    )
    used_web_search: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # AI-63 AC-03: citations stay attached to the answer that produced them.
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # AI-125 AC-01: suggested follow-up questions stay attached to the assistant answer.
    suggested_questions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # AI-63: a refused turn stays visible in the history UI but must never be
    # replayed as conversational context for later rewrites or answers.
    excluded_from_context: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    # Thinking metadata survives history reloads so "Thought for Xs" stays visible.
    thinking_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")
    source_ticket: Mapped["Ticket | None"] = relationship("Ticket", uselist=False, passive_deletes="all", lazy="selectin")
    parent_message: Mapped["ChatMessage | None"] = relationship(
        "ChatMessage", remote_side=[id], foreign_keys=[parent_message_id],
        lazy="joined", join_depth=1, passive_deletes="all",
    )

    @property
    def original_question(self) -> str | None:
        """Return the linked USER prompt only when it belongs to this session."""
        parent = self.parent_message
        if parent and parent.role == "USER" and parent.session_id == self.session_id:
            return parent.content
        return None

    @property
    def ticket_id(self) -> uuid.UUID | None:
        """Expose the feedback ticket ID in chat history responses."""
        return self.source_ticket.id if self.source_ticket else None

    @property
    def can_create_feedback_ticket(self) -> bool:
        """Require an assistant answer with an identifiable original question."""
        return self.role == "ASSISTANT" and self.original_question is not None
