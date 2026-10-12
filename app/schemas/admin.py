"""Pydantic schemas for admin: courses, classes, accounts, imports, email (AI-31, AI-105)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


# --- Courses (AC-01) ---
class CourseCreate(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=255)


class CourseRead(BaseModel):
    id: int
    code: str
    title: str
    class_count: int = 0

    model_config = {"from_attributes": True}


# --- Classes (AC-01, AC-02) ---
class ClassCreate(BaseModel):
    course_id: int
    code: str = Field(min_length=1, max_length=50)


class ClassAssign(BaseModel):
    trainer_id: int | None = None
    ta_id: int | None = None


class ClassRead(BaseModel):
    id: int
    course_id: int
    course_code: str = ""
    code: str
    trainer_id: int | None = None
    trainer_name: str | None = None
    ta_id: int | None = None
    ta_name: str | None = None
    student_count: int = 0
    # AC-12: delete warning data.
    document_count: int = 0
    ticket_count: int = 0

    model_config = {"from_attributes": True}


# --- Accounts (AC-03, AC-11, AC-12) ---
class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    email: EmailStr
    role: str = Field(min_length=1)
    class_code: str | None = Field(default=None, max_length=50)


class UserRead(BaseModel):
    id: int
    username: str
    email: str
    role: str
    class_id: int | None
    class_code: str | None = None
    is_active: bool

    model_config = {"from_attributes": True}


class UserDeactivate(BaseModel):
    is_active: bool


# --- Import (AC-05..AC-10) ---
class ImportRowRead(BaseModel):
    id: int
    row_number: int
    username: str
    email: str
    role_raw: str
    class_raw: str
    status: str
    message: str

    model_config = {"from_attributes": True}


class ImportBatchRead(BaseModel):
    id: int
    filename: str
    total_rows: int
    imported_count: int
    skipped_count: int
    failed_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportBatchDetail(ImportBatchRead):
    rows: list[ImportRowRead] = []


# --- Email queue (AC-08, AC-09) ---
class EmailQueueStats(BaseModel):
    pending: int
    sent: int
    failed: int


class EmailOutboxRead(BaseModel):
    id: int
    to_email: str
    subject: str
    status: str
    attempts: int
    last_error: str
    created_at: datetime
    sent_at: datetime | None = None

    model_config = {"from_attributes": True}


class EmailConfigRead(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_sender: str
    # Masked — never returned or logged (AC-08, AI-105).
    has_app_password: bool
    smtp_rate_per_minute: int
    email_dry_run: bool


class EmailConfigUpdate(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_sender: str | None = None
    # App password (Gmail / Google Workspace) — write-only.
    smtp_app_password: str | None = None
    smtp_rate_per_minute: int | None = Field(default=None, ge=1, le=500)
    email_dry_run: bool | None = None


class TestEmailRequest(BaseModel):
    to_email: EmailStr


# --- AI-105 runtime settings + connection checks ---
class LlmSettingsRead(BaseModel):
    """Expose the active model and whether its API key is configured."""

    model: str = Field(min_length=1)
    api_key_configured: bool


class EmailSettingsRead(BaseModel):
    """Expose SMTP metadata without returning the app password."""

    smtp_host: str = Field(min_length=1)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_security: Literal["STARTTLS"]
    sender_email: EmailStr | None
    rate_limit_per_minute: int = Field(ge=1)
    app_password_configured: bool


class AdminSettingsRead(BaseModel):
    """Group AI-105 LLM and email settings displayed to an Admin."""

    llm: LlmSettingsRead
    email: EmailSettingsRead


class ConnectionTestResult(BaseModel):
    """Report an external connection check without exposing credentials."""

    success: bool
    message: str = Field(min_length=1)
