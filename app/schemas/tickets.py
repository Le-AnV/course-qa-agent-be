"""Request and response schemas for student-initiated tickets (AI-81)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TicketStatus = Literal["OPEN", "ANSWERED", "RESOLVED"]


class TicketCreate(BaseModel):
    class_id: uuid.UUID
    question: str = Field(min_length=1, max_length=10000)
    agent_answer: str | None = Field(default=None, max_length=10000)
    details: str | None = Field(default=None, max_length=10000)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Question must not be empty.")
        return cleaned

    @field_validator("details", "agent_answer")
    @classmethod
    def normalize_details(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class TicketReplyCreate(BaseModel):
    message: str = Field(min_length=1, max_length=10000)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Message must not be empty.")
        return cleaned


class TicketReplyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    user_id: uuid.UUID
    message: str
    created_at: datetime


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    student_id: uuid.UUID
    question: str
    agent_answer: str | None = None
    status: TicketStatus
    created_at: datetime


class TicketInboxItem(TicketRead):
    student_name: str
    class_name: str
    class_code: str


class TicketListResponse(BaseModel):
    items: list[TicketInboxItem]
    total: int
    page: int
    page_size: int
