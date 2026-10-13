"""Pydantic schemas for Chat History module (AI-95, Epic E7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChatRole = Literal["USER", "ASSISTANT", "SYSTEM"]


class ChatMessageBase(BaseModel):
    """Base schema for chat message attributes."""

    role: ChatRole = Field(description="Role of the message sender (USER, ASSISTANT, SYSTEM)")
    content: str = Field(min_length=1, description="Text content of the message")
    used_web_search: bool = Field(
        default=False, description="Whether web search was utilized for generating the answer"
    )


class MessageCreateRequest(ChatMessageBase):
    """Request payload to append a message to an existing session."""

    pass


class ChatMessageResponse(ChatMessageBase):
    """Response payload for a single chat message (AC-02)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    created_at: datetime


class SessionCreateRequest(BaseModel):
    """Request payload to explicitly create a new chat session (AC-03)."""

    class_id: uuid.UUID = Field(description="UUID of the class the session belongs to")
    title: str | None = Field(default=None, max_length=255, description="Optional conversation title")


class ChatSessionResponse(BaseModel):
    """Response schema for chat session summary in sidebar/list (AC-04, AC-06)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ChatSessionListResponse(BaseModel):
    """Response schema for list of chat sessions."""

    items: list[ChatSessionResponse] = Field(default_factory=list)
    total: int = 0


class ChatDetailResponse(ChatSessionResponse):
    """Detailed response schema for a session including all ordered messages (AC-05, AC-07)."""

    messages: list[ChatMessageResponse] = Field(default_factory=list)


class DeleteSessionResponse(BaseModel):
    """Response schema for session deletion confirmation."""

    message: str = "Chat session deleted successfully."
