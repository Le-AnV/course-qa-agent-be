"""Pydantic schemas for admin: courses, classes, accounts, imports, email (AI-31, AI-105)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


# --- Courses (AC-01) ---
class CourseCreate(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=255)


class CourseRead(BaseModel):
    id: uuid.UUID
    code: str
    title: str
    class_count: int = 0

    model_config = {"from_attributes": True}


# --- Classes (standalone: class IS the subject) ---
class ClassPrerequisiteRead(BaseModel):
    """A class that must be passed before joining this one."""

    id: uuid.UUID
    code: str
    name: str


class ClassCreate(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    # ERD: class.trainer_id NOT NULL — trainer required at creation.
    trainer_id: uuid.UUID
    name: str | None = Field(default=None, max_length=100)
    semester: str = Field(default="S1", max_length=20)
    academic_year: str = Field(default="2026", max_length=20)
    # Manual credits entered at class creation; hard-capped 20/semester at enrollment.
    credits: int = Field(default=3, ge=1, le=6)
    # Roster capacity at creation. None = unlimited.
    capacity: int | None = Field(default=None, ge=1, le=500)
    # Subjects a student must have passed before joining this class.
    prerequisite_class_ids: list[uuid.UUID] = []


class ClassPrerequisiteUpdate(BaseModel):
    prerequisite_class_ids: list[uuid.UUID] = []


class ClassAssign(BaseModel):
    trainer_id: uuid.UUID | None = None


class ClassTrainerAdd(BaseModel):
    """Co-trainer to add (mid-semester handover: A starts, B takes over)."""

    trainer_id: uuid.UUID


class ClassTrainerRead(BaseModel):
    trainer_id: uuid.UUID
    name: str | None = None
    email: str | None = None
    # The primary trainer is the one stored in ``class.trainer_id``.
    is_primary: bool = False


class ClassRead(BaseModel):
    id: uuid.UUID
    code: str
    trainer_id: uuid.UUID | None = None
    trainer_name: str | None = None
    # Primary trainer first, then extra trainers on the class.
    trainers: list[ClassTrainerRead] = []
    credits: int = 3
    student_count: int = 0
    # P5: roster capacity (None = unlimited). P7: ACTIVE | ARCHIVED.
    capacity: int | None = None
    status: str = "ACTIVE"
    # AC-12: delete warning data.
    document_count: int = 0
    ticket_count: int = 0
    # Subjects a student must have passed before joining this class.
    prerequisites: list[ClassPrerequisiteRead] = []

    model_config = {"from_attributes": True}


class ClassStatusUpdate(BaseModel):
    """P7: archive hides a class from teachers/students; ACTIVE restores it."""

    status: Literal["ACTIVE", "ARCHIVED"]


class ClassConfigRead(BaseModel):
    class_id: uuid.UUID
    capacity: int | None = None
    delivery_mode: Literal["on_campus", "online", "hybrid"] = "on_campus"
    default_language: Literal["en", "vi"] = "en"
    answer_scope: Literal["syllabus_only", "syllabus_first", "open"] = "syllabus_first"
    citation_required: bool = True
    max_answer_words: int = 250
    session_length_minutes: int = 90


class ClassConfigUpdate(BaseModel):
    """Only the fields present in the request are changed.

    ``capacity=None`` clears the limit, so omit the key to leave it untouched.
    """

    capacity: int | None = Field(default=None, ge=1, le=500)
    delivery_mode: Literal["on_campus", "online", "hybrid"] | None = None
    default_language: Literal["en", "vi"] | None = None
    answer_scope: Literal["syllabus_only", "syllabus_first", "open"] | None = None
    citation_required: bool | None = None
    max_answer_words: int | None = Field(default=None, ge=50, le=1000)
    session_length_minutes: int | None = Field(default=None, ge=30, le=180)


class ClassConfigCopy(BaseModel):
    """P6: reuse another class' configuration as a template."""

    source_class_id: uuid.UUID


class ClassConfigSourceRead(BaseModel):
    """A class the caller may copy a configuration from."""

    id: uuid.UUID
    code: str
    name: str
    status: str = "ACTIVE"


class ClassInfoRead(BaseModel):
    """The class page a teacher fills in and students read (all fields optional)."""

    class_id: uuid.UUID
    description: str | None = None
    meeting_url: str | None = None
    group_url: str | None = None
    schedule_note: str | None = None
    note: str | None = None


class ClassInfoUpdate(BaseModel):
    """Only the fields present in the request are changed; "" clears a field."""

    description: str | None = Field(default=None, max_length=2000)
    meeting_url: str | None = Field(default=None, max_length=500)
    group_url: str | None = Field(default=None, max_length=500)
    # Matches the class_info column (String(500)): the old 200 cap silently
    # truncated multi-line schedules into one glued paragraph.
    schedule_note: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=2000)


class TemplateDocumentRead(BaseModel):
    """An approved source document the trainer may carry into the target class."""

    id: uuid.UUID
    file_name: str
    file_type: str
    size_bytes: int
    version: int


class TemplateDocumentSkip(BaseModel):
    file_name: str
    reason: str


class ClassTemplateCopy(BaseModel):
    """Copy selected config/info fields and approved documents from one class."""

    source_class_id: uuid.UUID
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    copy_config: bool = True
    info_fields: list[str] = Field(default_factory=list, max_length=5)


class ClassTemplateCopyRead(BaseModel):
    config: ClassConfigRead
    info: ClassInfoRead
    copied_documents: list[TemplateDocumentRead] = Field(default_factory=list)
    skipped_documents: list[TemplateDocumentSkip] = Field(default_factory=list)


# --- Accounts (AC-03, AC-11, AC-12) ---
class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    email: EmailStr
    role: str = Field(min_length=1)
    class_code: str | None = Field(default=None, max_length=50)
    # Optional MSSV for students; blank means auto-generate INITIALS_NNNN.
    student_code: str | None = Field(default=None, max_length=20)
    # Optional MSNV for staff; students must not send one.
    staff_code: str | None = Field(default=None, max_length=20)


class UserCodeGenerate(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1)


class UserCodeRead(BaseModel):
    code: str


class UserRead(BaseModel):
    id: uuid.UUID
    # Compat name — sourced from ``user.full_name`` (ERD has no username column).
    username: str
    email: str
    # Lowercase role alias for FE compat (DB stores UPPERCASE).
    role: str
    class_id: uuid.UUID | None
    class_code: str | None = None
    student_code: str | None = None
    staff_code: str | None = None
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
    student_code: str | None = None
    staff_code: str | None = None

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
    # Live drain progress (AC-09): the admin UI shows "running" + progress and
    # disables "Process queue now" until `draining` goes back to false.
    draining: bool = False
    drain_started_at: datetime | None = None
    drain_total: int = 0
    drain_done: int = 0
    drain_sent: int = 0
    drain_failed: int = 0


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


# --- AI-65 retrieval score threshold (admin-configurable, T35) ---
class RetrievalConfigRead(BaseModel):
    """Active KB-vs-Internet threshold plus the documented default."""

    threshold: float = Field(ge=0, le=1)
    default_threshold: float = Field(ge=0, le=1)
    is_default: bool
    updated_at: datetime | None = None


class RetrievalConfigUpdate(BaseModel):
    """New threshold; applies to the next question (AC-05)."""

    threshold: float = Field(ge=0, le=1)
