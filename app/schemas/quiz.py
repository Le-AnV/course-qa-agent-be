"""Request, response, and DTO schemas for Quiz and Assessment (AI-148)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QuizStatus = Literal["DRAFT", "PUBLISHED", "ARCHIVED"]
QuestionType = Literal["SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK"]
BloomLevel = Literal["REMEMBER", "UNDERSTAND", "APPLY"]
AttemptStatus = Literal["IN_PROGRESS", "SUBMITTED", "EXPIRED"]
OptionLabel = Literal["A", "B", "C", "D", "E", "F"]
QuizAvailability = Literal["UPCOMING", "OPEN", "CLOSED", "EXHAUSTED"]

VALID_OPTION_LABELS = ("A", "B", "C", "D", "E", "F")


def _as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime; SQLite hands back naive values."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


class QuizOptionBase(BaseModel):
    option_label: OptionLabel
    option_text: str = Field(min_length=1)

    @field_validator("option_text")
    @classmethod
    def clean_option_text(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Option text must not be empty.")
        return s


class QuizOptionCreate(QuizOptionBase):
    is_correct: bool = False
    order_index: int = 0


class QuizOptionTeacherRead(QuizOptionBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_correct: bool
    order_index: int


class QuizOptionStudentExamRead(QuizOptionBase):
    """Option view during active student exam session (hides is_correct)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_index: int


class QuizOptionStudentResultRead(QuizOptionBase):
    """Option view after submission in result review."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_correct: bool
    order_index: int


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


class QuizQuestionBase(BaseModel):
    question_type: QuestionType
    question: str = Field(min_length=1)
    points: float = Field(default=1.0, gt=0)
    difficulty: BloomLevel = "REMEMBER"
    order_index: int = 0
    rubric: str | None = None
    source_reference: str | None = None

    @field_validator("question")
    @classmethod
    def clean_question(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Question must not be empty.")
        return s


class QuizQuestionCreate(QuizQuestionBase):
    explanation: str | None = None
    grading_config: dict[str, Any] | None = None
    options: list[QuizOptionCreate] = Field(default_factory=list)
    correct_answers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_question_options(self) -> QuizQuestionCreate:
        q_type = self.question_type

        if q_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            if len(self.options) < 2:
                raise ValueError(
                    f"{q_type} requires at least 2 options (Option A and Option B)."
                )
            if len(self.options) > 6:
                raise ValueError(f"{q_type} allows at most 6 options (Option A through F).")

            # Validate sequential labels starting from A
            labels = [opt.option_label for opt in self.options]
            expected_labels = list(VALID_OPTION_LABELS[: len(self.options)])
            if labels != expected_labels:
                raise ValueError(
                    f"Options must use sequential labels starting from A (expected {expected_labels}, got {labels})."
                )

            correct_count = sum(1 for opt in self.options if opt.is_correct)

            if q_type == "SINGLE_CHOICE":
                if correct_count != 1:
                    raise ValueError(
                        f"SINGLE_CHOICE question must have exactly 1 correct option, found {correct_count}."
                    )
            elif q_type == "MULTIPLE_CHOICE" and correct_count < 1:
                raise ValueError(
                    "MULTIPLE_CHOICE question must have at least 1 correct option."
                )
        elif q_type == "FILL_IN_BLANK":
            if self.options:
                raise ValueError("FILL_IN_BLANK question must not define options A through F.")
            if not self.correct_answers and not (self.rubric and self.rubric.strip()):
                raise ValueError(
                    "FILL_IN_BLANK question requires at least one correct answer or rubric definition."
                )

        return self


class QuizQuestionTeacherRead(QuizQuestionBase):
    """Full question details for instructors and question-generation agents."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    explanation: str | None = None
    grading_config: dict[str, Any] | None = None
    options: list[QuizOptionTeacherRead] = Field(default_factory=list)


class QuizQuestionStudentExamRead(BaseModel):
    """Question presentation during exam session (hides answers, explanation, rubric, and source)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question_type: QuestionType
    question: str
    points: float
    difficulty: BloomLevel
    order_index: int
    options: list[QuizOptionStudentExamRead] = Field(default_factory=list)


class QuizQuestionStudentResultRead(BaseModel):
    """Question presentation after exam submission (reveals answers and explanations)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question_type: QuestionType
    question: str
    points: float
    difficulty: BloomLevel
    order_index: int
    explanation: str | None = None
    rubric: str | None = None
    source_reference: str | None = None
    options: list[QuizOptionStudentResultRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Quiz
# ---------------------------------------------------------------------------


class QuizBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    time_limit_minutes: int | None = Field(default=None, gt=0)
    passing_score: float | None = Field(default=None, ge=0)
    passing_percentage: float = Field(default=50.0, ge=0, le=100)
    # AI-153: NULL window bounds mean "no bound"; NULL max_attempts is unlimited.
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    max_attempts: int | None = Field(default=1, gt=0)
    status: QuizStatus = "DRAFT"

    @field_validator("title")
    @classmethod
    def clean_title(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Quiz title must not be empty.")
        return s

    @model_validator(mode="after")
    def validate_window(self) -> QuizBase:
        if (
            self.opens_at is not None
            and self.closes_at is not None
            and _as_utc(self.opens_at) >= _as_utc(self.closes_at)
        ):
            raise ValueError("opens_at must be earlier than closes_at.")
        return self


class QuizCreate(QuizBase):
    class_id: uuid.UUID
    questions: list[QuizQuestionCreate] = Field(default_factory=list)


class QuizUpdate(BaseModel):
    """Partial metadata update. ``status`` is deliberately absent (AI-151).

    Publishing only happens through ``POST /quizzes/{id}/approve`` so the approval
    audit trail can never be forged by a plain patch.
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    time_limit_minutes: int | None = Field(default=None, gt=0)
    passing_score: float | None = Field(default=None, ge=0)
    passing_percentage: float | None = Field(default=None, ge=0, le=100)
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    max_attempts: int | None = Field(default=None, gt=0)

    @field_validator("title")
    @classmethod
    def clean_title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("Quiz title must not be empty.")
        return s

    @model_validator(mode="after")
    def validate_window(self) -> QuizUpdate:
        if (
            self.opens_at is not None
            and self.closes_at is not None
            and _as_utc(self.opens_at) >= _as_utc(self.closes_at)
        ):
            raise ValueError("opens_at must be earlier than closes_at.")
        return self


class QuizTeacherRead(QuizBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    creator_id: uuid.UUID
    approved_by_id: uuid.UUID | None = None
    approved_at: datetime | None = None
    approved_by_name: str | None = None
    created_at: datetime
    updated_at: datetime
    questions: list[QuizQuestionTeacherRead] = Field(default_factory=list)
    question_count: int = 0
    total_points: float = 0.0
    question_type_counts: dict[str, int] = Field(default_factory=dict)


def quiz_teacher_read(quiz: Any, *, approved_by_name: str | None = None) -> QuizTeacherRead:
    """Build the teacher view with the totals AI-151 requires (AC-02)."""
    questions = list(quiz.questions or [])
    counts: dict[str, int] = {}
    total = 0.0
    for question in questions:
        counts[question.question_type] = counts.get(question.question_type, 0) + 1
        total += float(question.points or 0)
    payload = QuizTeacherRead.model_validate(quiz)
    return payload.model_copy(
        update={
            "approved_by_name": approved_by_name,
            "question_count": len(questions),
            "total_points": round(total, 2),
            "question_type_counts": counts,
        }
    )


class QuizSummaryRead(BaseModel):
    """Lightweight quiz row for the teacher import list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None = None
    status: QuizStatus
    time_limit_minutes: int | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    max_attempts: int | None = None
    question_count: int = 0
    created_at: datetime
    updated_at: datetime


class QuizStudentExamRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    title: str
    description: str | None = None
    time_limit_minutes: int | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    max_attempts: int | None = None
    questions: list[QuizQuestionStudentExamRead] = Field(default_factory=list)


class QuizStudentResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    title: str
    description: str | None = None
    time_limit_minutes: int | None = None
    passing_score: float | None = None
    passing_percentage: float | None = None
    questions: list[QuizQuestionStudentResultRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Attempts & Answers
# ---------------------------------------------------------------------------


class StudentAnswerSubmit(BaseModel):
    question_id: uuid.UUID
    selected_option_labels: list[OptionLabel] | None = None
    text_answer: str | None = None
    is_marked_for_review: bool = False

    @field_validator("selected_option_labels")
    @classmethod
    def clean_labels(cls, v: list[OptionLabel] | None) -> list[OptionLabel] | None:
        if v is None:
            return None
        # Remove duplicates while preserving order
        seen = set()
        cleaned = []
        for label in v:
            if label not in seen:
                seen.add(label)
                cleaned.append(label)
        return cleaned


class QuizAttemptSubmit(BaseModel):
    answers: list[StudentAnswerSubmit] = Field(default_factory=list)


class QuizAnswerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    attempt_id: uuid.UUID
    question_id: uuid.UUID
    selected_option_labels: list[str] | None = None
    text_answer: str | None = None
    is_marked_for_review: bool = False
    score_awarded: float | None = None
    breakdown: dict[str, Any] | None = None


class QuizAttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    quiz_id: uuid.UUID
    student_id: uuid.UUID
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None = None
    total_score: float | None = None
    is_passed: bool | None = None
    answers: list[QuizAnswerRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Results & statistics (AI-154)
# ---------------------------------------------------------------------------


class QuizStudentListRead(BaseModel):
    """One published quiz in a student's class quiz list.

    The attempt summary is scoped to the requesting student, so the list can
    show "not started" / "in progress" / their official score without leaking
    class-wide results.
    """

    id: uuid.UUID
    class_id: uuid.UUID
    title: str
    description: str | None = None
    question_count: int = 0
    total_points: float = 0.0
    time_limit_minutes: int | None = None
    passing_score: float | None = None
    passing_percentage: float | None = None
    attempt_count: int = 0
    submitted_count: int = 0
    has_in_progress: bool = False
    official_attempt_id: uuid.UUID | None = None
    official_score: float | None = None
    official_max_score: float | None = None
    official_percentage: float | None = None
    official_is_passed: bool | None = None
    official_submitted_at: datetime | None = None


class QuizAttemptHistoryRead(BaseModel):
    """One row in a student's attempt history for a quiz."""

    id: uuid.UUID
    quiz_id: uuid.UUID
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    total_score: float | None = None
    max_score: float | None = None
    passing_score: float | None = None
    passing_percentage: float | None = None
    percentage: float | None = None
    is_passed: bool | None = None
    official: bool = False


class QuizAnswerResultRead(BaseModel):
    """A student's own answer in a submitted attempt."""

    question_id: uuid.UUID
    selected_option_labels: list[str] | None = None
    text_answer: str | None = None
    score_awarded: float | None = None
    is_correct: bool | None = None


class QuizQuestionResultRead(BaseModel):
    """Question, correct answer, and student answer shown after submission."""

    id: uuid.UUID
    question_type: QuestionType
    question: str
    points: float
    order_index: int
    explanation: str | None = None
    options: list[QuizOptionStudentResultRead] = Field(default_factory=list)
    correct_option_labels: list[str] = Field(default_factory=list)
    accepted_text_answers: list[str] = Field(default_factory=list)
    answer: QuizAnswerResultRead | None = None


class QuizAttemptResultRead(BaseModel):
    """Full student result for one graded attempt."""

    id: uuid.UUID
    quiz_id: uuid.UUID
    quiz_title: str
    student_id: uuid.UUID
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    total_score: float | None = None
    max_score: float | None = None
    passing_score: float | None = None
    passing_percentage: float | None = None
    percentage: float | None = None
    is_passed: bool | None = None
    official: bool = False
    show_explanations: bool = False
    questions: list[QuizQuestionResultRead] = Field(default_factory=list)


class QuizSubmissionRead(BaseModel):
    """One submitted attempt in the teacher's submissions table."""

    attempt_id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    student_code: str | None = None
    student_email: str
    status: AttemptStatus
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    total_score: float | None = None
    max_score: float | None = None
    passing_percentage: float | None = None
    percentage: float | None = None
    is_passed: bool | None = None
    official: bool = False


class ScoreBandRead(BaseModel):
    band: str
    count: int


class QuizQuestionStatisticRead(BaseModel):
    question_id: uuid.UUID
    order_index: int
    question_type: QuestionType
    question: str
    points: float
    answered_count: int
    average_score: float
    full_correct_rate: float


class QuizStatisticsRead(BaseModel):
    """Class statistics computed from one official attempt per student."""

    quiz_id: uuid.UUID
    quiz_title: str
    submitted_count: int
    max_score: float
    average_score: float | None = None
    average_percentage: float | None = None
    pass_rate: float | None = None
    score_bands: list[ScoreBandRead] = Field(default_factory=list)
    per_question: list[QuizQuestionStatisticRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Student delivery (AI-153)
# ---------------------------------------------------------------------------


class StudentQuizListItem(BaseModel):
    """One published quiz as a student sees it, with availability and attempts."""

    quiz_id: uuid.UUID
    class_id: uuid.UUID
    class_code: str
    class_name: str
    title: str
    description: str | None = None
    time_limit_minutes: int | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    max_attempts: int | None = None
    attempts_used: int = 0
    attempts_left: int | None = None
    availability: QuizAvailability = "OPEN"
    attempt_id: uuid.UUID | None = None
    attempt_status: AttemptStatus | None = None
    can_start: bool = False
    total_score: float | None = None
    passing_percentage: float | None = None
    is_passed: bool | None = None


class StudentQuizListRead(BaseModel):
    server_time: datetime
    quizzes: list[StudentQuizListItem] = Field(default_factory=list)


class StudentAnswerDraft(BaseModel):
    """One autosaved answer; empty labels and text clear the draft answer."""

    question_id: uuid.UUID
    selected_option_labels: list[OptionLabel] | None = None
    text_answer: str | None = None
    is_marked_for_review: bool = False

    @field_validator("selected_option_labels")
    @classmethod
    def clean_labels(cls, v: list[OptionLabel] | None) -> list[OptionLabel] | None:
        if v is None:
            return None
        seen: set[str] = set()
        cleaned: list[OptionLabel] = []
        for label in v:
            if label not in seen:
                seen.add(label)
                cleaned.append(label)
        return cleaned

    @field_validator("text_answer")
    @classmethod
    def clean_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


class StudentAnswersSave(BaseModel):
    answers: list[StudentAnswerDraft] = Field(default_factory=list)


class QuizTakeResultRead(BaseModel):
    """Revealed only after submission (AC-05)."""

    attempt_id: uuid.UUID
    quiz_id: uuid.UUID
    quiz_title: str
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None = None
    total_score: float
    max_score: float
    percentage: float
    passing_score: float | None = None
    passing_percentage: float = 50.0
    is_passed: bool | None = None
    questions: list[QuizQuestionStudentResultRead] = Field(default_factory=list)
    answers: list[QuizAnswerRead] = Field(default_factory=list)


class QuizAttemptSessionRead(BaseModel):
    """Active exam payload + saved drafts, or the result once finalized."""

    attempt_id: uuid.UUID
    quiz_id: uuid.UUID
    quiz_title: str
    quiz_description: str | None = None
    time_limit_minutes: int | None = None
    status: AttemptStatus
    started_at: datetime
    deadline_at: datetime | None = None
    server_time: datetime
    remaining_seconds: int | None = None
    questions: list[QuizQuestionStudentExamRead] = Field(default_factory=list)
    answers: list[QuizAnswerRead] = Field(default_factory=list)
    total_points: float = 0.0
    answered_count: int = 0
    result: QuizTakeResultRead | None = None
