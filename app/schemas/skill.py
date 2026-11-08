"""Pydantic schemas for Class-scoped Skill Taxonomy and Rubrics."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SkillLevel = Literal["NOVICE", "BEGINNER", "COMPETENT", "PROFICIENT", "MASTER"]


class SkillLevelRubricBase(BaseModel):
    level: SkillLevel
    description: str = Field(min_length=1, description="Evaluation scope / tested understanding at this level")
    threshold_pct: int = Field(default=70, ge=1, le=100, description="Passing threshold percentage for this level")


class SkillLevelRubricCreate(SkillLevelRubricBase):
    pass


class SkillLevelRubricUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1)
    threshold_pct: int | None = Field(default=None, ge=1, le=100)


class SkillLevelRubricRead(SkillLevelRubricBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    skill_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class SkillBase(BaseModel):
    code: str = Field(min_length=1, max_length=100, description="Skill code, unique per class")
    name: str = Field(min_length=1, max_length=255, description="Display name of the skill")
    category: str = Field(default="GENERAL", min_length=1, max_length=100)
    description: str | None = None


class SkillCreate(SkillBase):
    rubrics: list[SkillLevelRubricCreate] = Field(default_factory=list)


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None


class SkillRead(SkillBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    rubrics: list[SkillLevelRubricRead] = Field(default_factory=list)
