"""Class-scoped Skill Taxonomy and Evaluation Rubrics (AI-Skill-Taxonomy)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.quiz import QuizQuestion

from sqlalchemy import (
    CheckConstraint,
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


class Skill(Base):
    """Skill entity scoped to a specific class, owned and managed by the class trainer."""

    __tablename__ = "skill"
    __table_args__ = (
        UniqueConstraint("class_id", "code", name="uq_skill_class_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("class.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(
        String(100), nullable=False, default="GENERAL", server_default="GENERAL", index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    rubrics: Mapped[list["SkillLevelRubric"]] = relationship(
        "SkillLevelRubric",
        back_populates="skill",
        cascade="all, delete-orphan",
        order_by="SkillLevelRubric.level",
    )
    questions: Mapped[list["QuizQuestion"]] = relationship(
        "QuizQuestion",
        back_populates="skill",
    )


class SkillLevelRubric(Base):
    """Evaluation scope defining student understanding required at each proficiency level."""

    __tablename__ = "skill_level_rubric"
    __table_args__ = (
        CheckConstraint(
            "level IN ('NOVICE', 'BEGINNER', 'COMPETENT', 'PROFICIENT', 'MASTER')",
            name="ck_skill_level_rubric_level",
        ),
        UniqueConstraint("skill_id", "level", name="uq_skill_level_rubric"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skill.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    # Nội dung đánh giá mức độ hiểu biết ở cấp độ này
    description: Mapped[str] = mapped_column(Text, nullable=False)
    threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=70)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    skill: Mapped[Skill] = relationship("Skill", back_populates="rubrics")
