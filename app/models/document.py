"""SQLAlchemy model for uploaded learning materials (Epic E2, AI-35/37/41)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
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
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Lifecycle mirrors the service statuses: pending -> processing -> indexed,
    # with rejected / failed / replaced as terminal states (Upload AC-02,
    # AI-55 pipeline; replaced = superseded by a newer version, AI-39).
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # AI-39 (AC-03): uploading a file name that already exists in the class
    # creates the next version; version 1 is the original upload.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    storage_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    clazz: Mapped["Class"] = relationship("Class", back_populates="documents")
    uploader: Mapped["User"] = relationship("User", back_populates="documents")
    syllabus_units: Mapped[list["SyllabusUnit"]] = relationship(
        "SyllabusUnit", back_populates="document", cascade="all, delete-orphan"
    )


class SyllabusUnit(Base):
    """Structured course unit extracted from an indexed syllabus document (AI-133)."""

    __tablename__ = "syllabus_unit"
    __table_args__ = (
        UniqueConstraint("class_id", "unit_number", name="uq_syllabus_unit_class_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    unit_number: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    topic_outline: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped["Document"] = relationship("Document", back_populates="syllabus_units")

