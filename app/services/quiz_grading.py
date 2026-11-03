"""Quiz Grading Engine with Partial Credit with Penalty support (AI-148)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QuestionGradingResult:
    """Detailed grading evaluation for a single question."""

    score: float
    max_points: float
    is_correct: bool
    breakdown: dict[str, Any] = field(default_factory=dict)


def normalize_fill_in_text(text: str | None, case_sensitive: bool = False) -> str:
    """Normalize text for fill-in-the-blank comparison: collapse whitespace, trim."""
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text.strip())
    return normalized if case_sensitive else normalized.lower()


def parse_fill_in_aliases(raw_answers: list[str] | str | None) -> list[str]:
    """Parse comma/pipe/newline separated list of accepted fill-in answers."""
    if not raw_answers:
        return []
    if isinstance(raw_answers, list):
        aliases = []
        for item in raw_answers:
            aliases.extend(parse_fill_in_aliases(item))
        return aliases

    # Split by pipe or newline, or clean strings
    parts = re.split(r"[|\n]", raw_answers)
    return [p.strip() for p in parts if p.strip()]


def grade_single_choice(
    correct_label: str | None,
    selected_labels: list[str] | None,
    points: float = 1.0,
) -> QuestionGradingResult:
    """Grade a SINGLE_CHOICE question (binary 0 or points)."""
    selected = (selected_labels or [])
    selected_label = selected[0] if selected else None

    if not correct_label or not selected_label:
        return QuestionGradingResult(
            score=0.0,
            max_points=points,
            is_correct=False,
            breakdown={
                "question_type": "SINGLE_CHOICE",
                "selected": selected_label,
                "correct": correct_label,
            },
        )

    is_correct = selected_label.upper() == correct_label.upper()
    score = points if is_correct else 0.0

    return QuestionGradingResult(
        score=score,
        max_points=points,
        is_correct=is_correct,
        breakdown={
            "question_type": "SINGLE_CHOICE",
            "selected": selected_label,
            "correct": correct_label,
            "matched": is_correct,
        },
    )


def grade_multiple_choice(
    correct_labels: set[str] | list[str],
    all_option_labels: set[str] | list[str],
    selected_labels: set[str] | list[str] | None,
    points: float = 1.0,
    grading_config: dict[str, Any] | None = None,
) -> QuestionGradingResult:
    """Grade a MULTIPLE_CHOICE question using Partial Credit with Penalty.

    Algorithm:
    - k = number of correct options (k >= 1).
    - correct_selected = items in selected that are in correct_labels.
    - wrong_selected = items in selected that are NOT in correct_labels.
    - score = ((correct_selected - penalty_ratio * wrong_selected) / k) * points.
    - If no options selected: score = 0.0.
    - If allow_negative is False, score = max(0.0, score).
    """
    config = grading_config or {}
    allow_negative = config.get("allow_negative", True)
    penalty_ratio = float(config.get("penalty_ratio", 1.0))
    floor_score = config.get("floor_score", None)

    correct_set = {str(c).strip().upper() for c in (correct_labels or []) if str(c).strip()}
    selected_set = {str(s).strip().upper() for s in (selected_labels or []) if str(s).strip()}

    k = len(correct_set)
    if k == 0:
        return QuestionGradingResult(
            score=0.0,
            max_points=points,
            is_correct=False,
            breakdown={"error": "No correct options defined for question."},
        )

    if not selected_set:
        return QuestionGradingResult(
            score=0.0,
            max_points=points,
            is_correct=False,
            breakdown={
                "question_type": "MULTIPLE_CHOICE",
                "k": k,
                "selected_count": 0,
                "correct_selected": [],
                "wrong_selected": [],
                "raw_score": 0.0,
            },
        )

    correct_selected = sorted(selected_set.intersection(correct_set))
    wrong_selected = sorted(selected_set.difference(correct_set))

    # Formula: (correct_count - penalty_ratio * wrong_count) / k * points
    c_count = len(correct_selected)
    w_count = len(wrong_selected)
    fraction = (c_count - (penalty_ratio * w_count)) / k
    raw_score = round(fraction * points, 4)

    score = raw_score
    if not allow_negative and score < 0.0:
        score = 0.0
    if floor_score is not None and score < float(floor_score):
        score = float(floor_score)

    is_full_correct = (c_count == k) and (w_count == 0)

    return QuestionGradingResult(
        score=score,
        max_points=points,
        is_correct=is_full_correct,
        breakdown={
            "question_type": "MULTIPLE_CHOICE",
            "k": k,
            "correct_options": sorted(correct_set),
            "selected_options": sorted(selected_set),
            "correct_selected": correct_selected,
            "wrong_selected": wrong_selected,
            "fraction": round(fraction, 4),
            "penalty_ratio": penalty_ratio,
            "allow_negative": allow_negative,
            "raw_score": raw_score,
            "final_score": score,
        },
    )


def grade_fill_in_blank(
    accepted_answers: list[str] | str,
    student_text: str | None,
    points: float = 1.0,
    grading_config: dict[str, Any] | None = None,
) -> QuestionGradingResult:
    """Grade a FILL_IN_BLANK question comparing student text against accepted answers."""
    config = grading_config or {}
    case_sensitive = config.get("case_sensitive", False)

    aliases = parse_fill_in_aliases(accepted_answers)
    normalized_aliases = [normalize_fill_in_text(a, case_sensitive) for a in aliases]
    normalized_student = normalize_fill_in_text(student_text, case_sensitive)

    is_match = bool(normalized_student and normalized_student in normalized_aliases)
    score = points if is_match else 0.0

    return QuestionGradingResult(
        score=score,
        max_points=points,
        is_correct=is_match,
        breakdown={
            "question_type": "FILL_IN_BLANK",
            "student_text": student_text,
            "normalized_student": normalized_student,
            "accepted_aliases": aliases,
            "case_sensitive": case_sensitive,
            "matched": is_match,
        },
    )


def grade_question_submission(
    question_type: str,
    points: float,
    options_data: list[dict[str, Any]] | None,
    student_selected_labels: list[str] | None,
    student_text: str | None,
    accepted_text_answers: list[str] | str | None = None,
    grading_config: dict[str, Any] | None = None,
) -> QuestionGradingResult:
    """Convenience router for grading any question type."""
    q_type = question_type.upper()

    if q_type == "SINGLE_CHOICE":
        correct_label = None
        for opt in (options_data or []):
            if opt.get("is_correct"):
                correct_label = opt.get("option_label")
                break
        return grade_single_choice(
            correct_label=correct_label,
            selected_labels=student_selected_labels,
            points=points,
        )

    if q_type == "MULTIPLE_CHOICE":
        correct_labels = [
            opt.get("option_label")
            for opt in (options_data or [])
            if opt.get("is_correct") and opt.get("option_label")
        ]
        all_labels = [
            opt.get("option_label")
            for opt in (options_data or [])
            if opt.get("option_label")
        ]
        return grade_multiple_choice(
            correct_labels=correct_labels,
            all_option_labels=all_labels,
            selected_labels=student_selected_labels,
            points=points,
            grading_config=grading_config,
        )

    if q_type == "FILL_IN_BLANK":
        answers = accepted_text_answers or []
        return grade_fill_in_blank(
            accepted_answers=answers,
            student_text=student_text,
            points=points,
            grading_config=grading_config,
        )

    raise ValueError(f"Unsupported question type: {question_type}")
