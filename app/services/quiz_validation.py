"""Reusable validation and bounded regeneration for generated quiz questions (AI-150).

The generator itself belongs to AI-149. This module is the policy layer: it turns
raw generator output into explicit per-question issues, removes duplicates, checks
the requested composition, and asks for replacements only for the missing slots.

Deterministic duplicate policy
------------------------------
Questions are compared after Unicode NFKC normalisation, case folding, punctuation
removal and whitespace collapsing. Exact matches are always duplicates. Near
duplicates use :class:`difflib.SequenceMatcher` on the normalised strings; the
default ratio is :data:`NEAR_DUPLICATE_THRESHOLD` (0.9) and callers can override it
per :class:`ValidationSpec`. No embedding service or new dependency is involved.
"""

from __future__ import annotations

import math
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.schemas.quiz import QuestionType, QuizQuestionCreate

NEAR_DUPLICATE_THRESHOLD = 0.9
DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.7
MAX_REGENERATION_ATTEMPTS = 3
SUPPORTED_QUESTION_TYPES = set(get_args(QuestionType))

DUPLICATE_CODES = {"DUPLICATE", "NEAR_DUPLICATE", "DUPLICATE_EXISTING"}
NON_BLOCKING_CODES = {"INVALID_CONFIDENCE"}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    code: str
    message: str
    field: str | None = None
    question_index: int | None = None


class GeneratedQuestion(BaseModel):
    """One raw generator result plus optional generator-reported confidence."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data: dict[str, Any] | QuizQuestionCreate
    confidence: Any = None


class ValidationSpec(BaseModel):
    """The requested composition of a generated draft.

    ``type_counts`` is the exact per-type composition. ``total_questions`` is the
    exact overall count; when both are present the caller should make them agree.
    """

    total_questions: int | None = Field(default=None, ge=0)
    type_counts: dict[str, int] = Field(default_factory=dict)
    low_confidence_threshold: float = Field(
        default=DEFAULT_LOW_CONFIDENCE_THRESHOLD, ge=0.0, le=1.0
    )
    near_duplicate_threshold: float = Field(
        default=NEAR_DUPLICATE_THRESHOLD, ge=0.0, le=1.0
    )

    @model_validator(mode="after")
    def validate_exact_counts(self) -> ValidationSpec:
        unsupported_types = self.type_counts.keys() - SUPPORTED_QUESTION_TYPES
        if unsupported_types:
            raise ValueError(
                "Unsupported question type(s): "
                + ", ".join(sorted(unsupported_types))
            )
        if any(count < 0 for count in self.type_counts.values()):
            raise ValueError("type_counts values must be non-negative.")
        if self.total_questions is not None and self.type_counts:
            requested = sum(self.type_counts.values())
            if requested != self.total_questions:
                raise ValueError(
                    "total_questions must equal sum(type_counts): "
                    f"{self.total_questions} != {requested}."
                )
        return self


class QuestionValidationResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    index: int
    accepted: bool
    question: QuizQuestionCreate | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    confidence: float | None = None
    duplicate_of: int | None = None


class BatchSummary(BaseModel):
    requested_total: int | None = None
    requested_type_counts: dict[str, int] = Field(default_factory=dict)
    accepted_total: int = 0
    discarded_total: int = 0
    flagged_total: int = 0
    type_counts: dict[str, int] = Field(default_factory=dict)
    shortfall: dict[str, int] = Field(default_factory=dict)
    total_shortfall: int = 0
    duplicates_removed: int = 0
    low_confidence: int = 0
    attempts: int = 0
    regenerated: int = 0
    issues: list[ValidationIssue] = Field(default_factory=list)
    requires_teacher_approval: bool = True


class BatchValidationResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    questions: list[QuizQuestionCreate] = Field(default_factory=list)
    results: list[QuestionValidationResult] = Field(default_factory=list)
    summary: BatchSummary = Field(default_factory=BatchSummary)


@dataclass(frozen=True)
class MissingSlot:
    """One slot the generator should fill, with the reason it is missing."""

    question_type: str
    reason: str
    source_index: int | None = None
    difficulty: str | None = None


@dataclass
class RegenerationResult:
    validation: BatchValidationResult
    attempts: int
    regenerated: int
    generator_issues: list[ValidationIssue]


Generator = Callable[[list[MissingSlot]], Sequence[GeneratedQuestion]]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_PUNCTUATION = {
    ord(char): " "
    for char in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~“”‘’—–…"
}


def normalize_question_text(text: str) -> str:
    """Normalise a question for deterministic duplicate comparisons."""

    normalised = unicodedata.normalize("NFKC", text).casefold()
    normalised = normalised.translate(_PUNCTUATION)
    return " ".join(normalised.split())


def normalize_option_text(text: str) -> str:
    """Normalise option text without erasing code-significant symbols.

    Question near-duplicate matching may drop punctuation, but options such as
    ``C++``, ``C#``, ``x++`` and ``x--`` are distinct answers. Options therefore
    keep punctuation; case, whitespace and Unicode width are still normalised.
    """

    normalised = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalised.split())


def question_similarity(first: str, second: str) -> float:
    """Return the deterministic similarity ratio in the range 0..1."""

    return SequenceMatcher(
        None,
        normalize_question_text(first),
        normalize_question_text(second),
    ).ratio()


def _issue(
    code: str,
    message: str,
    *,
    field: str | None = None,
    index: int | None = None,
) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, field=field, question_index=index)


def _parse_question(
    raw: dict[str, Any] | QuizQuestionCreate, index: int
) -> tuple[QuizQuestionCreate | None, list[ValidationIssue]]:
    """Parse a raw generator result and collect all schema issues it exposes."""

    if isinstance(raw, QuizQuestionCreate):
        return raw, []

    try:
        return QuizQuestionCreate.model_validate(raw), []
    except ValidationError as exc:
        issues = []
        for error in exc.errors():
            field = ".".join(str(part) for part in error.get("loc", ())) or None
            issues.append(
                _issue(
                    "INVALID_SCHEMA",
                    str(error.get("msg", "Invalid question payload.")),
                    field=field,
                    index=index,
                )
            )
        return None, issues


def _extra_issues(
    question: QuizQuestionCreate, index: int
) -> list[ValidationIssue]:
    """Checks the AI-148 model intentionally leaves to the generation layer."""

    issues: list[ValidationIssue] = []

    if question.question_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
        labels = {option.option_label for option in question.options}
        declared_answers = {
            label.strip().upper()
            for label in question.correct_answers
            if label.strip()
        }
        missing_labels = [
            label
            for label in question.correct_answers
            if label.strip().upper() not in labels
        ]
        if missing_labels:
            issues.append(
                _issue(
                    "CORRECT_ANSWER_NOT_IN_OPTIONS",
                    "Correct answer labels are not in the option list: "
                    + ", ".join(missing_labels),
                    field="correct_answers",
                    index=index,
                )
            )
        elif declared_answers:
            marked_correct = {
                option.option_label
                for option in question.options
                if option.is_correct
            }
            if declared_answers != marked_correct:
                issues.append(
                    _issue(
                        "CORRECT_ANSWER_MISMATCH",
                        "Correct answer labels contradict the options marked is_correct: "
                        f"{sorted(declared_answers)} != {sorted(marked_correct)}.",
                        field="correct_answers",
                        index=index,
                    )
                )

        seen_options: set[str] = set()
        duplicate_labels: list[str] = []
        for option in question.options:
            normalized_option = normalize_option_text(option.option_text)
            if normalized_option in seen_options:
                duplicate_labels.append(option.option_label)
            else:
                seen_options.add(normalized_option)
        if duplicate_labels:
            issues.append(
                _issue(
                    "DUPLICATE_OPTION_TEXT",
                    "Option text is duplicated for label(s): "
                    + ", ".join(duplicate_labels),
                    field="options",
                    index=index,
                )
            )
    elif question.question_type == "FILL_IN_BLANK":
        # Fill-in-the-blank answers live in ``correct_answers`` and are persisted
        # into ``grading_config["correct_answers"]`` by the review service.
        if any(not answer.strip() for answer in question.correct_answers):
            issues.append(
                _issue(
                    "INVALID_ANSWER",
                    "Fill-in-the-blank answers must not be blank.",
                    field="correct_answers",
                    index=index,
                )
            )

    return issues


def _confidence_issues(
    confidence: Any, spec: ValidationSpec, index: int
) -> tuple[list[ValidationIssue], list[str], float | None]:
    if confidence is None:
        return [], [], None

    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or confidence < 0.0
        or confidence > 1.0
    ):
        return (
            [
                _issue(
                    "INVALID_CONFIDENCE",
                    "Confidence must be a number between 0 and 1.",
                    field="confidence",
                    index=index,
                )
            ],
            [],
            None,
        )

    flags = ["LOW_CONFIDENCE"] if confidence < spec.low_confidence_threshold else []
    return [], flags, float(confidence)


def _normalised_existing(
    existing: Iterable[dict[str, Any] | QuizQuestionCreate],
) -> list[str]:
    normalised: list[str] = []
    for raw in existing:
        question, _ = _parse_question(raw, -1)
        if question is not None:
            normalised.append(normalize_question_text(question.question))
    return normalised


def _find_duplicate(
    text: str,
    accepted: list[QuizQuestionCreate],
    accepted_normalised: list[str],
    accepted_indices: list[int],
    existing_normalised: list[str],
    threshold: float,
) -> tuple[str | None, int | None]:
    normalised = normalize_question_text(text)

    for candidate_index, known in enumerate(accepted_normalised):
        if normalised == known:
            return "DUPLICATE", accepted_indices[candidate_index]
        if question_similarity(text, accepted[candidate_index].question) >= threshold:
            return "NEAR_DUPLICATE", accepted_indices[candidate_index]

    for known in existing_normalised:
        if normalised == known:
            return "DUPLICATE_EXISTING", None
        if question_similarity(text, known) >= threshold:
            return "DUPLICATE_EXISTING", None

    return None, None


# ---------------------------------------------------------------------------
# Batch validation
# ---------------------------------------------------------------------------


def validate_batch(
    candidates: Sequence[GeneratedQuestion],
    spec: ValidationSpec,
    *,
    existing: Iterable[dict[str, Any] | QuizQuestionCreate] = (),
) -> BatchValidationResult:
    """Validate a generated batch and return explicit per-question outcomes.

    Invalid or duplicate items are never included in ``questions``. Low-confidence
    but otherwise valid items stay in ``questions`` and carry a flag so the review
    screen can mark them for teacher attention.
    """

    accepted: list[QuizQuestionCreate] = []
    accepted_normalised: list[str] = []
    accepted_indices: list[int] = []
    results: list[QuestionValidationResult] = []
    type_counts: Counter[str] = Counter()
    existing_normalised = _normalised_existing(existing)

    for index, candidate in enumerate(candidates):
        question, parse_issues = _parse_question(candidate.data, index)
        issues: list[ValidationIssue] = list(parse_issues)
        flags: list[str] = []
        confidence: float | None = None

        if question is not None:
            issues.extend(_extra_issues(question, index))
            confidence_issues, confidence_flags, confidence = _confidence_issues(
                candidate.confidence, spec, index
            )
            issues.extend(confidence_issues)
            flags.extend(confidence_flags)

        blocking = [issue for issue in issues if issue.code not in NON_BLOCKING_CODES]

        if question is None or blocking:
            results.append(
                QuestionValidationResult(
                    index=index,
                    accepted=False,
                    question=question,
                    issues=issues,
                    flags=flags,
                    confidence=confidence,
                )
            )
            continue

        if spec.type_counts and question.question_type not in spec.type_counts:
            issues.append(
                _issue(
                    "TYPE_NOT_REQUESTED",
                    f"Question type {question.question_type} was not requested.",
                    field="question_type",
                    index=index,
                )
            )
            results.append(
                QuestionValidationResult(
                    index=index,
                    accepted=False,
                    question=question,
                    issues=issues,
                    flags=flags,
                    confidence=confidence,
                )
            )
            continue

        duplicate_code, duplicate_of = _find_duplicate(
            question.question,
            accepted,
            accepted_normalised,
            accepted_indices,
            existing_normalised,
            spec.near_duplicate_threshold,
        )
        if duplicate_code is not None:
            issues.append(
                _issue(
                    duplicate_code,
                    "Question duplicates another question in the draft."
                    if duplicate_of is not None
                    else "Question duplicates an existing accepted question.",
                    field="question",
                    index=index,
                )
            )
            results.append(
                QuestionValidationResult(
                    index=index,
                    accepted=False,
                    question=question,
                    issues=issues,
                    flags=flags,
                    confidence=confidence,
                    duplicate_of=duplicate_of,
                )
            )
            continue

        requested_for_type = spec.type_counts.get(question.question_type)
        if requested_for_type is not None and type_counts[question.question_type] >= requested_for_type:
            issues.append(
                _issue(
                    "COUNT_EXCEEDED",
                    f"More {question.question_type} questions than requested.",
                    field="question_type",
                    index=index,
                )
            )
            results.append(
                QuestionValidationResult(
                    index=index,
                    accepted=False,
                    question=question,
                    issues=issues,
                    flags=flags,
                    confidence=confidence,
                )
            )
            continue

        if spec.total_questions is not None and len(accepted) >= spec.total_questions:
            issues.append(
                _issue(
                    "COUNT_EXCEEDED",
                    "More questions than the requested total.",
                    field=None,
                    index=index,
                )
            )
            results.append(
                QuestionValidationResult(
                    index=index,
                    accepted=False,
                    question=question,
                    issues=issues,
                    flags=flags,
                    confidence=confidence,
                )
            )
            continue

        accepted.append(question)
        accepted_normalised.append(normalize_question_text(question.question))
        accepted_indices.append(index)
        type_counts[question.question_type] += 1
        results.append(
            QuestionValidationResult(
                index=index,
                accepted=True,
                question=question,
                issues=issues,
                flags=flags,
                confidence=confidence,
            )
        )

    summary = _summarise(accepted, results, spec)
    return BatchValidationResult(questions=accepted, results=results, summary=summary)


def _summarise(
    accepted: Sequence[QuizQuestionCreate],
    results: Sequence[QuestionValidationResult],
    spec: ValidationSpec,
    *,
    attempts: int = 0,
    regenerated: int = 0,
    issues: Sequence[ValidationIssue] = (),
) -> BatchSummary:
    type_counts = Counter(question.question_type for question in accepted)
    shortfall = {
        question_type: max(0, requested - type_counts[question_type])
        for question_type, requested in spec.type_counts.items()
    }
    if spec.total_questions is not None:
        total_shortfall = max(0, spec.total_questions - len(accepted))
    else:
        total_shortfall = sum(shortfall.values())

    discarded = [result for result in results if not result.accepted]
    flagged = [result for result in results if result.accepted and result.flags]

    return BatchSummary(
        requested_total=spec.total_questions,
        requested_type_counts=dict(spec.type_counts),
        accepted_total=len(accepted),
        discarded_total=len(discarded),
        flagged_total=len(flagged),
        type_counts=dict(type_counts),
        shortfall={key: value for key, value in shortfall.items() if value > 0},
        total_shortfall=total_shortfall,
        duplicates_removed=sum(
            1
            for result in discarded
            if any(issue.code in DUPLICATE_CODES for issue in result.issues)
        ),
        low_confidence=sum(1 for result in flagged if "LOW_CONFIDENCE" in result.flags),
        attempts=attempts,
        regenerated=regenerated,
        issues=list(issues),
        requires_teacher_approval=True,
    )


# ---------------------------------------------------------------------------
# Bounded regeneration
# ---------------------------------------------------------------------------


def _missing_slots(
    accepted: Sequence[QuizQuestionCreate],
    results: Sequence[QuestionValidationResult],
    spec: ValidationSpec,
) -> list[MissingSlot]:
    type_counts = Counter(question.question_type for question in accepted)
    slots: list[MissingSlot] = []

    if spec.type_counts:
        for question_type, requested in spec.type_counts.items():
            missing = max(0, requested - type_counts[question_type])
            if not missing:
                continue
            rejected = [
                result
                for result in results
                if not result.accepted and result.question is not None
                and result.question.question_type == question_type
            ]
            reasons = [
                "; ".join(issue.code for issue in result.issues) or "INVALID"
                for result in rejected
            ]
            for slot_index in range(missing):
                reason = reasons[slot_index] if slot_index < len(reasons) else "MISSING_COUNT"
                slots.append(
                    MissingSlot(
                        question_type=question_type,
                        reason=reason,
                        source_index=rejected[slot_index].index
                        if slot_index < len(rejected)
                        else None,
                    )
                )
        return slots

    if spec.total_questions is not None:
        missing = max(0, spec.total_questions - len(accepted))
        slots.extend(
            MissingSlot(question_type="ANY", reason="MISSING_COUNT")
            for _ in range(missing)
        )
    return slots


def validate_and_regenerate(
    candidates: Sequence[GeneratedQuestion],
    spec: ValidationSpec,
    generator: Generator,
    *,
    max_attempts: int = MAX_REGENERATION_ATTEMPTS,
    existing: Iterable[dict[str, Any] | QuizQuestionCreate] = (),
) -> RegenerationResult:
    """Validate a draft, then request only missing slots with bounded attempts.

    The function is pure: it never writes to the database and never replaces an
    accepted question. Invalid replacement candidates are discarded and reported;
    valid low-confidence candidates are kept with a review flag.
    """

    existing_items = list(existing)
    initial = validate_batch(candidates, spec, existing=existing_items)
    accepted = list(initial.questions)
    results = list(initial.results)
    generator_issues: list[ValidationIssue] = []
    attempts = 0
    regenerated = 0
    attempt_budget = max(0, min(max_attempts, MAX_REGENERATION_ATTEMPTS))

    while attempts < attempt_budget:
        slots = _missing_slots(accepted, results, spec)
        if not slots:
            break

        attempts += 1
        try:
            produced = list(generator(slots))
            produced = [
                item if isinstance(item, GeneratedQuestion) else GeneratedQuestion(data=item)
                for item in produced
            ]
        except Exception as exc:  # noqa: BLE001 - generator boundary
            generator_issues.append(
                _issue(
                    "GENERATOR_ERROR",
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        if not produced:
            generator_issues.append(
                _issue("GENERATOR_EMPTY", "The generator returned no questions.")
            )
            continue

        missing_by_type = Counter(slot.question_type for slot in slots)
        sub_spec = ValidationSpec(
            total_questions=len(slots) if not spec.type_counts else None,
            type_counts={
                question_type: count
                for question_type, count in missing_by_type.items()
                if question_type != "ANY"
            },
            low_confidence_threshold=spec.low_confidence_threshold,
            near_duplicate_threshold=spec.near_duplicate_threshold,
        )
        sub = validate_batch(produced, sub_spec, existing=[*accepted, *existing_items])

        offset = len(results)
        for result in sub.results:
            results.append(
                result.model_copy(
                    update={
                        "index": result.index + offset,
                        "duplicate_of": (
                            result.duplicate_of + offset
                            if result.duplicate_of is not None
                            else None
                        ),
                        "issues": [
                            issue.model_copy(
                                update={
                                    "question_index": (
                                        issue.question_index + offset
                                        if issue.question_index is not None
                                        else None
                                    )
                                }
                            )
                            for issue in result.issues
                        ],
                    }
                )
            )
        accepted.extend(sub.questions)
        regenerated += len(sub.questions)

    summary = _summarise(
        accepted,
        results,
        spec,
        attempts=attempts,
        regenerated=regenerated,
        issues=generator_issues,
    )
    return RegenerationResult(
        validation=BatchValidationResult(
            questions=accepted,
            results=results,
            summary=summary,
        ),
        attempts=attempts,
        regenerated=regenerated,
        generator_issues=generator_issues,
    )
