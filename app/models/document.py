"""SQLAlchemy model for uploaded learning materials (Epic E2, AI-35/37/41)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.course import Class
    from app.models.user import User


class Document(Base):
    __tablename__ = "document"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Owning class for class-scoped materials (``scope == "class"``); NULL for
    # course-scoped materials, which every class of the subject shares.
    class_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # "course" = shared with every class of the subject; "class" = private to
    # the class named by ``class_id`` (a class may upload its own extra files).
    scope: Mapped[str] = mapped_column(
        String(10), nullable=False, default="course", server_default="course", index=True
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Lifecycle mirrors the service statuses: pending -> processing -> indexed,
    # with rejected / failed as terminal states (Upload AC-02, AI-55 pipeline).
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # Optional reviewer note stored when a document is rejected (Phase 1 plan v3).
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)
    # Course-shared versioning (plan v3 Phase 2a). Evolved in place on this
    # table; per-version rows live in DocumentVersion below.
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("course.id"), nullable=True, index=True, default=None
    )
    # Uploader of v1 (D7); later versions record their own uploaded_by.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id"), nullable=True, default=None
    )
    # Lowercase + trim + NFC normalized file name: version-group identity (D3).
    name_normalized: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True, default=None
    )
    # App-managed pointer to the serving document_versions.id (no DB FK).
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    # Name clash across classes of one course: human must decide, never auto-merge.
    needs_review: Mapped[bool] = mapped_column(default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    clazz: Mapped["Class"] = relationship("Class", back_populates="documents")
    uploader: Mapped["User"] = relationship(
        "User", back_populates="documents", foreign_keys="Document.uploaded_by"
    )
    versions: Mapped[list["DocumentVersion"]] = relationship(
        "DocumentVersion", back_populates="document", cascade="all, delete-orphan"
    )
    events: Mapped[list["DocumentEvent"]] = relationship(
        "DocumentEvent", back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base):
    """One version inside a document group (plan v3 Phase 2a).

    Numbers grow monotonically and are never reused, including rejected
    versions. Only one version per group is the serving one (pointed to by
    ``Document.current_version_id``).
    """

    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_no"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(nullable=False, default=1)
    file_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Single review-note name across phases (D7): backfill copies
    # ``document.review_note`` here; Phase 2b writes here directly.
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id"), nullable=True, default=None
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id"), nullable=True, default=None
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    document: Mapped["Document"] = relationship("Document", back_populates="versions")


class DocumentEvent(Base):
    """Audit trail for every document/version transition (plan v3 Phase 2a).

    RAG consumes ``current_version_id`` + these events later; the backend
    never calls Qdrant directly.
    """

    __tablename__ = "document_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int | None] = mapped_column(nullable=True, default=None)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped["Document"] = relationship("Document", back_populates="events")


class DocumentClassOptout(Base):
    """Classes that removed a subject-shared document from their own list.

    A document still belongs to its course: removing it only detaches the
    current class (a row here), while every other class of the subject keeps
    seeing it. Deleting the row (Restore) brings it back to this class.
    Class-only materials (``document.scope == "class"``) are never opted out.
    """

    __tablename__ = "document_class_optout"
    __table_args__ = (PrimaryKeyConstraint("document_id", "class_id"),)

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False
    )
    removed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id"), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
