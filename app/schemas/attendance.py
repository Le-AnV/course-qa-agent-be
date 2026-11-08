"""Pydantic schemas for attendance, schedules, and sessions (AI-136)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AttendanceStatus = Literal["present", "late", "excused_absence", "unexcused_absence"]
SessionStatus = Literal["scheduled", "cancelled", "rescheduled"]


class ClassScheduleCreate(BaseModel):
    """Payload to create a recurring weekly schedule slot."""

    weekday: int = Field(ge=0, le=6, description="0=Monday ... 6=Sunday")
    start_time: time
    end_time: time
    effective_from: date
    effective_to: date | None = None
    trainer_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_window(self) -> ClassScheduleCreate:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time.")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not be before effective_from.")
        return self


class ClassScheduleRead(BaseModel):
    """Response DTO representing a recurring weekly schedule slot."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    subject: str
    weekday: int
    start_time: time
    end_time: time
    trainer_id: uuid.UUID
    trainer_name: str | None = None
    effective_from: date
    effective_to: date | None


class ClassSessionRead(BaseModel):
    """Response DTO representing a materialised dated class session."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    schedule_id: uuid.UUID | None
    date: date
    start_at: datetime
    end_at: datetime
    trainer_id: uuid.UUID
    trainer_name: str | None = None
    status: SessionStatus
    note: str | None = None
    student_count: int = 0
    marked_count: int = 0


class SessionCreate(BaseModel):
    """Payload to create an ad-hoc (unscheduled) dated session."""

    date: date
    start_time: time
    end_time: time
    trainer_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_times(self) -> SessionCreate:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time.")
        return self


class AttendanceRecordUpdate(BaseModel):
    """Single student attendance entry for updating an attendance sheet."""

    student_id: uuid.UUID
    status: AttendanceStatus
    note: str | None = Field(default=None, max_length=500)


class AttendanceBulkUpdate(BaseModel):
    """Bulk update payload for an attendance sheet."""

    records: list[AttendanceRecordUpdate] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def no_duplicates(self) -> AttendanceBulkUpdate:
        ids = [r.student_id for r in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate student_id in records.")
        return self


class AttendanceRecordRead(BaseModel):
    """Single student's attendance record in an attendance sheet."""

    student_id: uuid.UUID
    student_code: str | None = None
    full_name: str
    email: str
    status: AttendanceStatus | None = None
    note: str | None = None
    updated_at: datetime | None = None


class AttendanceCounts(BaseModel):
    """Count summary of attendance statuses for a session."""

    present: int = 0
    late: int = 0
    excused_absence: int = 0
    unexcused_absence: int = 0
    unmarked: int = 0


class AttendanceSheetRead(BaseModel):
    """Full attendance sheet for a class session."""

    session: ClassSessionRead
    summary: AttendanceCounts
    records: list[AttendanceRecordRead]


class AttendanceSummaryRow(BaseModel):
    """Summary of one student's attendance over a requested date range."""

    student_id: uuid.UUID
    student_code: str | None = None
    full_name: str
    present: int = 0
    late: int = 0
    excused_absence: int = 0
    unexcused_absence: int = 0
    unmarked: int = 0
    attendance_rate: float | None = None


class AttendanceSummaryRead(BaseModel):
    """Class-level attendance rate summary across all enrolled students."""

    class_id: uuid.UUID
    date_from: date
    date_to: date
    total_sessions: int
    students: list[AttendanceSummaryRow]


class StudentAttendanceItem(BaseModel):
    """Single session entry in a student's personal attendance history."""

    session_id: uuid.UUID
    date: date
    start_at: datetime
    end_at: datetime
    session_status: SessionStatus
    status: AttendanceStatus | None = None
    note: str | None = None
    updated_at: datetime | None = None


class StudentAttendanceRead(BaseModel):
    """Student's personal attendance history for a class."""

    class_id: uuid.UUID
    date_from: date
    date_to: date
    total_sessions: int
    attended: int
    attendance_rate: float | None = None
    items: list[StudentAttendanceItem]

