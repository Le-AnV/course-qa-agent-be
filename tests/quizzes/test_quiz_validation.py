"""Tests for reusable generated-question validation (AI-150)."""

from __future__ import annotations

from math import nan

import pytest
from pydantic import ValidationError

from app.services.quiz_validation import (
    GeneratedQuestion,
    MissingSlot,
    ValidationSpec,
    normalize_question_text,
    question_similarity,
    validate_and_regenerate,
    validate_batch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _single(question: str = "Which layer routes packets?", **overrides) -> dict:
    payload = {
        "question_type": "SINGLE_CHOICE",
        "question": question,
        "points": 1.0,
        "difficulty": "REMEMBER",
        "options": [
            {"option_label": "A", "option_text": "Network", "is_correct": True},
            {"option_label": "B", "option_text": "Physical", "is_correct": False},
            {"option_label": "C", "option_text": "Session", "is_correct": False},
        ],
    }
    payload.update(overrides)
    return payload


def _multiple(question: str = "Which are transport protocols?", **overrides) -> dict:
    payload = {
        "question_type": "MULTIPLE_CHOICE",
        "question": question,
        "points": 1.0,
        "difficulty": "UNDERSTAND",
        "options": [
            {"option_label": "A", "option_text": "TCP", "is_correct": True},
            {"option_label": "B", "option_text": "UDP", "is_correct": True},
            {"option_label": "C", "option_text": "IP", "is_correct": False},
        ],
    }
    payload.update(overrides)
    return payload


def _fill(question: str = "Which protocol secures HTTP?", **overrides) -> dict:
    payload = {
        "question_type": "FILL_IN_BLANK",
        "question": question,
        "points": 2.0,
        "difficulty": "APPLY",
        "correct_answers": ["HTTPS", "HTTP over TLS"],
    }
    payload.update(overrides)
    return payload


def _candidate(payload: dict, confidence: object = None) -> GeneratedQuestion:
    return GeneratedQuestion(data=payload, confidence=confidence)


def test_validation_spec_rejects_total_lower_than_type_counts() -> None:
    with pytest.raises(ValidationError, match="total_questions must equal"):
        ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 2})


def test_validation_spec_rejects_total_higher_than_type_counts() -> None:
    with pytest.raises(ValidationError, match="total_questions must equal"):
        ValidationSpec(total_questions=3, type_counts={"SINGLE_CHOICE": 2})


def test_validation_spec_rejects_negative_type_count() -> None:
    with pytest.raises(ValidationError, match="non-negative"):
        ValidationSpec(total_questions=0, type_counts={"SINGLE_CHOICE": -1})


def test_validation_spec_rejects_unsupported_question_type() -> None:
    with pytest.raises(ValidationError, match="Unsupported question type"):
        ValidationSpec(total_questions=1, type_counts={"ESSAY": 1})


# ---------------------------------------------------------------------------
# Normalisation and similarity
# ---------------------------------------------------------------------------


def test_normalise_removes_case_punctuation_and_spacing() -> None:
    assert normalize_question_text("  What IS HTTP?!  ") == "what is http"


def test_near_duplicate_threshold_is_deterministic() -> None:
    first = "Which protocol is used for secure web browsing?"
    second = "Which protocol is used for secure web browsing today?"
    third = "Name the three states of matter."

    assert question_similarity(first, second) > 0.9
    assert question_similarity(first, second) >= 0.9
    assert question_similarity(first, third) < 0.9


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------


def test_valid_mixed_batch_matches_exact_counts_and_types() -> None:
    spec = ValidationSpec(
        total_questions=3,
        type_counts={"SINGLE_CHOICE": 1, "FILL_IN_BLANK": 1, "MULTIPLE_CHOICE": 1},
    )
    result = validate_batch(
        [_candidate(_single()), _candidate(_fill()), _candidate(_multiple())],
        spec,
    )

    assert [q.question_type for q in result.questions] == [
        "SINGLE_CHOICE",
        "FILL_IN_BLANK",
        "MULTIPLE_CHOICE",
    ]
    assert result.summary.accepted_total == 3
    assert result.summary.discarded_total == 0
    assert result.summary.shortfall == {}
    assert result.summary.requires_teacher_approval is True
    assert all(item.accepted for item in result.results)


def test_malformed_schema_is_reported_per_question_not_accepted() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    result = validate_batch([_candidate({"question_type": "SINGLE_CHOICE"})], spec)

    assert result.questions == []
    assert result.summary.accepted_total == 0
    assert result.summary.shortfall == {"SINGLE_CHOICE": 1}
    codes = {issue.code for issue in result.results[0].issues}
    assert "INVALID_SCHEMA" in codes


def test_single_choice_with_two_correct_options_is_rejected() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(
        options=[
            {"option_label": "A", "option_text": "Network", "is_correct": True},
            {"option_label": "B", "option_text": "Physical", "is_correct": True},
        ]
    )
    result = validate_batch([_candidate(payload)], spec)

    assert result.questions == []
    assert any(issue.code == "INVALID_SCHEMA" for issue in result.results[0].issues)


def test_choice_correct_answer_must_exist_in_option_list() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(correct_answers=["D"])
    result = validate_batch([_candidate(payload)], spec)

    assert result.questions == []
    assert any(
        issue.code == "CORRECT_ANSWER_NOT_IN_OPTIONS"
        for issue in result.results[0].issues
    )


def test_fill_in_blank_answers_are_accepted_without_options() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"FILL_IN_BLANK": 1})
    result = validate_batch([_candidate(_fill())], spec)

    assert len(result.questions) == 1
    question = result.questions[0]
    assert question.question_type == "FILL_IN_BLANK"
    assert question.correct_answers == ["HTTPS", "HTTP over TLS"]
    assert question.options == []


def test_duplicate_option_text_is_rejected() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(
        options=[
            {"option_label": "A", "option_text": "Network", "is_correct": True},
            {"option_label": "B", "option_text": "network", "is_correct": False},
        ]
    )
    result = validate_batch([_candidate(payload)], spec)

    assert result.questions == []
    assert any(
        issue.code == "DUPLICATE_OPTION_TEXT" for issue in result.results[0].issues
    )


def test_code_symbols_distinguish_choice_options() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(
        options=[
            {"option_label": "A", "option_text": "C++", "is_correct": True},
            {"option_label": "B", "option_text": "C#", "is_correct": False},
        ]
    )
    result = validate_batch([_candidate(payload)], spec)

    assert result.summary.accepted_total == 1
    assert result.questions[0].options[0].option_text == "C++"


def test_operator_symbols_distinguish_choice_options() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(
        options=[
            {"option_label": "A", "option_text": "x++", "is_correct": True},
            {"option_label": "B", "option_text": "x--", "is_correct": False},
        ]
    )
    result = validate_batch([_candidate(payload)], spec)

    assert result.summary.accepted_total == 1
    assert result.questions[0].options[0].option_text == "x++"


def test_duplicate_option_still_detects_spacing() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(
        options=[
            {"option_label": "A", "option_text": "Network", "is_correct": True},
            {"option_label": "B", "option_text": " Ｎｅｔｗｏｒｋ ", "is_correct": False},
        ]
    )
    result = validate_batch([_candidate(payload)], spec)

    assert result.questions == []
    assert any(
        issue.code == "DUPLICATE_OPTION_TEXT" for issue in result.results[0].issues
    )


def test_duplicate_option_still_detects_unicode_width() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(
        options=[
            {"option_label": "A", "option_text": "Network", "is_correct": True},
            {
                "option_label": "B",
                "option_text": "\uff2e\uff45\uff54\uff57\uff4f\uff52\uff4b",
                "is_correct": False,
            },
        ]
    )
    result = validate_batch([_candidate(payload)], spec)

    assert result.questions == []
    assert any(
        issue.code == "DUPLICATE_OPTION_TEXT" for issue in result.results[0].issues
    )


def test_option_word_boundaries_are_significant() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(
        options=[
            {"option_label": "A", "option_text": "a b", "is_correct": True},
            {"option_label": "B", "option_text": "ab", "is_correct": False},
        ]
    )
    result = validate_batch([_candidate(payload)], spec)

    assert result.summary.accepted_total == 1
    assert result.questions[0].options[0].option_text == "a b"


def test_multiword_option_whitespace_still_duplicates() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(
        options=[
            {"option_label": "A", "option_text": "Machine Learning", "is_correct": True},
            {
                "option_label": "B",
                "option_text": "  machine \t  learning  ",
                "is_correct": False,
            },
        ]
    )
    result = validate_batch([_candidate(payload)], spec)

    assert result.questions == []
    assert any(
        issue.code == "DUPLICATE_OPTION_TEXT" for issue in result.results[0].issues
    )


def test_choice_correct_answers_mismatch_is_rejected() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    payload = _single(correct_answers=["B"])
    result = validate_batch([_candidate(payload)], spec)

    assert result.questions == []
    assert any(
        issue.code == "CORRECT_ANSWER_MISMATCH"
        for issue in result.results[0].issues
    )


def test_multiple_choice_correct_answers_mismatch_is_rejected() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"MULTIPLE_CHOICE": 1})
    payload = _multiple(correct_answers=["A"])
    result = validate_batch([_candidate(payload)], spec)

    assert result.questions == []
    assert any(
        issue.code == "CORRECT_ANSWER_MISMATCH"
        for issue in result.results[0].issues
    )


def test_choice_correct_answers_matching_flags_is_accepted() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 1, "MULTIPLE_CHOICE": 1})
    result = validate_batch(
        [
            _candidate(_single(correct_answers=["A"])),
            _candidate(_multiple(correct_answers=["A", "B"])),
        ],
        spec,
    )

    assert result.summary.accepted_total == 2
    assert result.summary.discarded_total == 0


def test_type_not_requested_is_discarded() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    result = validate_batch([_candidate(_fill())], spec)

    assert result.questions == []
    assert any(
        issue.code == "TYPE_NOT_REQUESTED" for issue in result.results[0].issues
    )


def test_extra_valid_questions_are_discarded_to_keep_exact_count() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    result = validate_batch(
        [_candidate(_single("First prompt")), _candidate(_single("Second prompt"))],
        spec,
    )

    assert result.summary.accepted_total == 1
    assert result.summary.discarded_total == 1
    assert any(
        issue.code == "COUNT_EXCEEDED" for issue in result.results[1].issues
    )


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_exact_duplicates_keep_first_and_report_duplicate_of() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 2})
    result = validate_batch(
        [
            _candidate(_single("What is HTTP?")),
            _candidate(_single("what is http")),
        ],
        spec,
    )

    assert result.summary.accepted_total == 1
    assert result.summary.duplicates_removed == 1
    assert result.results[1].duplicate_of == 0
    assert any(issue.code == "DUPLICATE" for issue in result.results[1].issues)


def test_duplicate_of_uses_original_index_when_earlier_candidate_was_rejected() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 2})
    result = validate_batch(
        [
            _candidate({"question": "malformed"}),
            _candidate(_single("What is HTTP?")),
            _candidate(_single("what is http")),
        ],
        spec,
    )

    assert result.results[2].duplicate_of == 1


def test_regenerated_duplicate_of_uses_final_result_index() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 2})

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        return [
            _candidate(_single("First replacement")),
            _candidate(_single("first replacement")),
        ]

    result = validate_and_regenerate(
        [_candidate({"question": "malformed"})], spec, generator, max_attempts=1
    )

    assert result.validation.results[2].duplicate_of == 1


def test_regenerated_duplicate_of_uses_final_index_after_sub_batch_reject() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 2})

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        return [
            _candidate({"question": "malformed replacement"}),
            _candidate(_single("Accepted replacement")),
            _candidate(_single("accepted replacement")),
        ]

    result = validate_and_regenerate(
        [_candidate({"question": "malformed initial"})], spec, generator, max_attempts=1
    )

    duplicate_result = result.validation.results[3]
    assert duplicate_result.duplicate_of == 2
    assert duplicate_result.index == 3


def test_near_duplicate_is_removed_with_configured_threshold() -> None:
    spec = ValidationSpec(
        total_questions=2,
        type_counts={"SINGLE_CHOICE": 2},
        near_duplicate_threshold=0.9,
    )
    result = validate_batch(
        [
            _candidate(_single("Which protocol is used for secure web browsing?")),
            _candidate(_single("Which protocol is used for secure web browsing today?")),
        ],
        spec,
    )

    assert result.summary.accepted_total == 1
    assert any(
        issue.code == "NEAR_DUPLICATE" for issue in result.results[1].issues
    )


def test_duplicate_against_existing_accepted_questions_is_removed() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    result = validate_batch(
        [_candidate(_single("Which layer routes packets?"))],
        spec,
        existing=[_single("Which layer routes packets?")],
    )

    assert result.questions == []
    assert any(
        issue.code == "DUPLICATE_EXISTING" for issue in result.results[0].issues
    )


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def test_low_confidence_question_is_kept_and_flagged() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    result = validate_batch([_candidate(_single(), confidence=0.4)], spec)

    assert len(result.questions) == 1
    assert result.results[0].accepted is True
    assert result.results[0].flags == ["LOW_CONFIDENCE"]
    assert result.results[0].confidence == 0.4
    assert result.summary.low_confidence == 1
    assert result.summary.flagged_total == 1


def test_missing_confidence_is_not_invented() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    result = validate_batch([_candidate(_single(), confidence=None)], spec)

    assert result.results[0].confidence is None
    assert result.results[0].flags == []


def test_invalid_confidence_is_reported_without_blocking_question() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    result = validate_batch([_candidate(_single(), confidence=1.5)], spec)

    assert len(result.questions) == 1
    assert result.results[0].confidence is None
    assert any(
        issue.code == "INVALID_CONFIDENCE" for issue in result.results[0].issues
    )


@pytest.mark.parametrize("confidence", [True, "0.4", nan])
def test_malformed_confidence_is_not_coerced_or_persisted(confidence: object) -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    result = validate_batch([_candidate(_single(), confidence=confidence)], spec)

    assert len(result.questions) == 1
    assert result.results[0].confidence is None
    assert result.results[0].flags == []
    assert any(issue.code == "INVALID_CONFIDENCE" for issue in result.results[0].issues)


# ---------------------------------------------------------------------------
# Bounded regeneration
# ---------------------------------------------------------------------------


def test_regeneration_fills_only_missing_slots_after_invalid_item() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 2})
    calls: list[list[MissingSlot]] = []

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        calls.append(missing)
        return [_candidate(_single("A regenerated prompt"))]

    result = validate_and_regenerate(
        [_candidate(_single("First valid prompt")), _candidate({"question": "broken"})],
        spec,
        generator,
        max_attempts=2,
    )

    assert result.validation.summary.accepted_total == 2
    assert result.validation.summary.shortfall == {}
    assert result.attempts == 1
    assert result.regenerated == 1
    assert len(calls) == 1
    assert calls[0][0].question_type == "SINGLE_CHOICE"
    assert "INVALID_SCHEMA" in calls[0][0].reason or "MISSING" in calls[0][0].reason


def test_regeneration_retries_duplicate_then_accepts_unique() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 2})
    answers = [
        [_candidate(_single("Duplicate of the accepted prompt"))],
        [_candidate(_single("A fresh regenerated prompt"))],
    ]
    calls = 0

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        nonlocal calls
        calls += 1
        return answers.pop(0)

    result = validate_and_regenerate(
        [_candidate(_single("Duplicate of the accepted prompt"))],
        spec,
        generator,
        max_attempts=2,
    )

    assert result.validation.summary.accepted_total == 2
    assert result.attempts == 2
    assert result.regenerated == 1
    assert calls == 2


def test_regeneration_keeps_low_confidence_replacement_and_flags() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        return [_candidate(_single("Low confidence replacement"), confidence=0.2)]

    result = validate_and_regenerate([], spec, generator, max_attempts=1)

    assert result.validation.summary.accepted_total == 1
    assert result.validation.summary.low_confidence == 1
    assert result.validation.results[-1].flags == ["LOW_CONFIDENCE"]
    assert result.validation.summary.shortfall == {}


def test_regeneration_accepts_raw_payloads_from_generator() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})

    def generator(missing: list[MissingSlot]) -> list[dict]:
        return [_single("Raw payload replacement")]

    result = validate_and_regenerate([], spec, generator, max_attempts=1)

    assert result.validation.summary.accepted_total == 1
    assert result.validation.questions[0].question == "Raw payload replacement"
    assert result.validation.summary.shortfall == {}


def test_regeneration_partial_response_reports_shortfall() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 2})

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        return [_candidate(_single("Only one of two requested questions"))]

    result = validate_and_regenerate([], spec, generator, max_attempts=1)

    assert result.validation.summary.accepted_total == 1
    assert result.validation.summary.shortfall == {"SINGLE_CHOICE": 1}
    assert result.validation.summary.total_shortfall == 1
    assert result.attempts == 1


def test_regeneration_caps_attempts_at_hard_maximum() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})
    calls = 0

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        nonlocal calls
        calls += 1
        return []

    result = validate_and_regenerate([], spec, generator, max_attempts=99)

    assert result.attempts == 3
    assert calls == 3
    assert result.validation.summary.shortfall == {"SINGLE_CHOICE": 1}


def test_regeneration_exhaustion_reports_shortfall_and_keeps_valid_questions() -> None:
    spec = ValidationSpec(total_questions=2, type_counts={"SINGLE_CHOICE": 2})
    calls = 0

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        nonlocal calls
        calls += 1
        raise TimeoutError("generator timed out")

    result = validate_and_regenerate(
        [_candidate(_single("The only valid prompt"))],
        spec,
        generator,
        max_attempts=2,
    )

    assert result.validation.summary.accepted_total == 1
    assert result.validation.summary.shortfall == {"SINGLE_CHOICE": 1}
    assert result.validation.summary.total_shortfall == 1
    assert result.attempts == 2
    assert calls == 2
    assert result.generator_issues
    assert result.generator_issues[0].code == "GENERATOR_ERROR"


def test_regeneration_never_accepts_malformed_or_extra_items() -> None:
    spec = ValidationSpec(total_questions=1, type_counts={"SINGLE_CHOICE": 1})

    def generator(missing: list[MissingSlot]) -> list[GeneratedQuestion]:
        return [
            _candidate({"question": "partial"}),
            _candidate(_fill("An unrequested type")),
        ]

    result = validate_and_regenerate([], spec, generator, max_attempts=1)

    assert result.validation.questions == []
    assert result.validation.summary.shortfall == {"SINGLE_CHOICE": 1}
    assert result.validation.summary.discarded_total == 2
    assert result.validation.summary.requires_teacher_approval is True
