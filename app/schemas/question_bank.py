"""Pydantic schemas for Question Bank management, generation, and import/export (AI-160)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

QuestionTypeLiteral = Literal["SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK"]
DifficultyLiteral = Literal[
    "NOVICE", "BEGINNER", "COMPETENT", "PROFICIENT", "MASTER", "REMEMBER", "UNDERSTAND", "APPLY"
]
SourceTypeLiteral = Literal["DOCUMENT", "CHAT_MISCONCEPTION", "MANUAL", "EXCEL_IMPORT"]
StatusLiteral = Literal["DRAFT", "APPROVED", "ARCHIVED"]
OptionLabelLiteral = Literal["A", "B", "C", "D", "E", "F"]


class BankQuestionOptionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    option_label: OptionLabelLiteral
    option_text: str = Field(..., min_length=1)
    is_correct: bool = False
    order_index: int = 0


class BankQuestionOptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question_id: uuid.UUID
    option_label: str
    option_text: str
    is_correct: bool
    order_index: int


class BankQuestionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question_type: QuestionTypeLiteral
    question: str = Field(..., min_length=1)
    points: float = Field(default=1.0, gt=0)
    difficulty: DifficultyLiteral = "NOVICE"
    skill_id: uuid.UUID | None = None
    explanation: str | None = None
    rubric: str | None = None
    source_type: SourceTypeLiteral = "MANUAL"
    source_document_id: uuid.UUID | None = None
    source_reference: str | None = None
    status: StatusLiteral = "APPROVED"
    options: list[BankQuestionOptionCreate] = []
    correct_answers: list[str] | None = None
    grading_config: dict[str, Any] | None = None


class BankQuestionUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question_type: QuestionTypeLiteral | None = None
    question: str | None = None
    points: float | None = Field(default=None, gt=0)
    difficulty: DifficultyLiteral | None = None
    skill_id: uuid.UUID | None = None
    explanation: str | None = None
    rubric: str | None = None
    source_type: SourceTypeLiteral | None = None
    source_document_id: uuid.UUID | None = None
    source_reference: str | None = None
    status: StatusLiteral | None = None
    options: list[BankQuestionOptionCreate] | None = None
    correct_answers: list[str] | None = None
    grading_config: dict[str, Any] | None = None


class BankQuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    skill_id: uuid.UUID | None
    skill_code: str | None = None
    skill_name: str | None = None
    created_by_id: uuid.UUID | None
    question_type: str
    question: str
    points: float
    difficulty: str
    explanation: str | None
    rubric: str | None
    source_type: str
    source_document_id: uuid.UUID | None
    source_document_name: str | None = None
    source_reference: str | None
    status: str
    grading_config: dict[str, Any] | None
    correct_answers: list[str] | None = None
    options: list[BankQuestionOptionRead] = []
    created_at: datetime
    updated_at: datetime


class BankQuestionListResponse(BaseModel):
    total: int
    items: list[BankQuestionRead]
    counts_by_difficulty: dict[str, int] = {}
    counts_by_type: dict[str, int] = {}
    counts_by_status: dict[str, int] = {}


class DocumentQuestionGenRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    document_ids: list[uuid.UUID] = Field(default_factory=list)
    skill_ids: list[uuid.UUID] = Field(default_factory=list)
    question_types: list[QuestionTypeLiteral] = Field(
        default_factory=lambda: ["SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK"]
    )
    difficulty_levels: list[DifficultyLiteral] = Field(
        default_factory=lambda: ["NOVICE", "BEGINNER", "COMPETENT", "PROFICIENT", "MASTER"]
    )
    questions_per_level: dict[str, int] | None = None
    num_questions: int = Field(default=5, ge=1, le=50)


class MisconceptionGenRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timeframe_days: int = Field(default=14, ge=1, le=90)
    skill_ids: list[uuid.UUID] = Field(default_factory=list)
    questions_per_level: dict[str, int] | None = None
    num_questions: int = Field(default=5, ge=1, le=30)


class BulkStatusUpdateRequest(BaseModel):
    question_ids: list[uuid.UUID]
    status: StatusLiteral

