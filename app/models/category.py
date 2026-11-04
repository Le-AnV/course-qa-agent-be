"""SQLAlchemy model for ClassCategory (AI-91, Epic E6).

Supports a 2-level hierarchical category taxonomy per class:
- Level 1: Topic / Subject area (parent_id is None)
- Level 2: Subtopic / Unit (parent_id references Level 1 category)
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.course import Class


class ClassCategory(Base):
    """Represents a knowledge domain category for classifying class materials and student queries."""

    __tablename__ = "class_category"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("class_category.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    clazz: Mapped["Class"] = relationship("Class", back_populates="categories")
    parent: Mapped["ClassCategory | None"] = relationship(
        "ClassCategory",
        remote_side=[id],
        back_populates="subcategories",
    )
    subcategories: Mapped[list["ClassCategory"]] = relationship(
        "ClassCategory",
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="ClassCategory.name",
    )
