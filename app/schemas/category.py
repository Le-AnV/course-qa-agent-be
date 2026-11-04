"""Pydantic schemas for Class Category management (AI-91, Epic E6)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CategoryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Category name")
    parent_id: uuid.UUID | None = Field(
        default=None, description="Parent category ID for Level 2 subtopics; None for Level 1 topics"
    )


class CategoryCreate(CategoryBase):
    pass


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str
    chunk_count: int = 0
    status: str = "active"
    created_at: datetime
    updated_at: datetime
    subcategories: list["CategoryRead"] = []


class CategoryTreeResponse(BaseModel):
    items: list[CategoryRead]
    total_categories: int
    total_chunks: int


class CategoryGenerateResponse(BaseModel):
    message: str
    generated_categories_count: int
    categories: list[CategoryRead]
