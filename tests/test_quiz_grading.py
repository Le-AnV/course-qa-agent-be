"""Unit tests for Quiz Grading Engine and Partial Credit with Penalty (AI-148)."""


from app.services.quiz_grading import (
    grade_fill_in_blank,
    grade_multiple_choice,
    grade_question_submission,
    grade_single_choice,
)

# ---------------------------------------------------------------------------
# SINGLE_CHOICE Tests
# ---------------------------------------------------------------------------


def test_grade_single_choice_correct():
    result = grade_single_choice(
        correct_label="A",
        selected_labels=["A"],
        points=2.0,
    )
    assert result.is_correct is True
    assert result.score == 2.0
    assert result.max_points == 2.0


def test_grade_single_choice_wrong():
    result = grade_single_choice(
        correct_label="A",
        selected_labels=["B"],
        points=2.0,
    )
    assert result.is_correct is False
    assert result.score == 0.0


def test_grade_single_choice_empty():
    result = grade_single_choice(
        correct_label="A",
        selected_labels=[],
        points=1.0,
    )
    assert result.is_correct is False
    assert result.score == 0.0


def test_grade_single_choice_case_insensitive():
    result = grade_single_choice(
        correct_label="c",
        selected_labels=["C"],
        points=1.5,
    )
    assert result.is_correct is True
    assert result.score == 1.5


# ---------------------------------------------------------------------------
# MULTIPLE_CHOICE Tests - Partial Credit with Penalty
# ---------------------------------------------------------------------------


def test_grade_multiple_choice_user_specification_matrix():
    """Verify the exact user-specified test matrix:

    Question with 5 choices, correct answers: A, C (k = 2, points = 1.0).
    | Selected | Expected Score |
    |---|---|
    | A, C | 1.0 |
    | A | 0.5 |
    | A, B | 0.0 (0.5 - 0.5) |
    | B | -0.5 |
    | (empty) | 0.0 |
    """
    all_options = ["A", "B", "C", "D", "E"]
    correct_options = ["A", "C"]

    # 1. Full correct: A, C
    r1 = grade_multiple_choice(
        correct_labels=correct_options,
        all_option_labels=all_options,
        selected_labels=["A", "C"],
        points=1.0,
    )
    assert r1.is_correct is True
    assert r1.score == 1.0
    assert r1.breakdown["k"] == 2
    assert r1.breakdown["correct_selected"] == ["A", "C"]
    assert r1.breakdown["wrong_selected"] == []

    # 2. Only A (partial credit): 1 / 2 = 0.5
    r2 = grade_multiple_choice(
        correct_labels=correct_options,
        all_option_labels=all_options,
        selected_labels=["A"],
        points=1.0,
    )
    assert r2.is_correct is False
    assert r2.score == 0.5
    assert r2.breakdown["correct_selected"] == ["A"]
    assert r2.breakdown["wrong_selected"] == []

    # 3. A, B (one correct, one wrong): 0.5 - 0.5 = 0.0
    r3 = grade_multiple_choice(
        correct_labels=correct_options,
        all_option_labels=all_options,
        selected_labels=["A", "B"],
        points=1.0,
    )
    assert r3.is_correct is False
    assert r3.score == 0.0
    assert r3.breakdown["correct_selected"] == ["A"]
    assert r3.breakdown["wrong_selected"] == ["B"]

    # 4. B (only wrong): 0 - 0.5 = -0.5 (negative score allowed by default)
    r4 = grade_multiple_choice(
        correct_labels=correct_options,
        all_option_labels=all_options,
        selected_labels=["B"],
        points=1.0,
    )
    assert r4.is_correct is False
    assert r4.score == -0.5
    assert r4.breakdown["correct_selected"] == []
    assert r4.breakdown["wrong_selected"] == ["B"]

    # 5. Empty selection: 0.0
    r5 = grade_multiple_choice(
        correct_labels=correct_options,
        all_option_labels=all_options,
        selected_labels=[],
        points=1.0,
    )
    assert r5.is_correct is False
    assert r5.score == 0.0
    assert r5.breakdown["selected_count"] == 0


def test_grade_multiple_choice_three_correct_answers():
    # 6 options, 3 correct: A, B, D (k = 3), points = 3.0
    # Each correct = +1.0, each wrong = -1.0
    correct = ["A", "B", "D"]
    all_opts = ["A", "B", "C", "D", "E", "F"]

    # Select A, B, E -> 2 correct, 1 wrong -> (2 - 1)/3 * 3 = 1.0
    res = grade_multiple_choice(
        correct_labels=correct,
        all_option_labels=all_opts,
        selected_labels=["A", "B", "E"],
        points=3.0,
    )
    assert res.score == 1.0
    assert res.is_correct is False


def test_grade_multiple_choice_disallow_negative():
    # If grading_config sets allow_negative=False, negative scores clamp to 0.0
    res = grade_multiple_choice(
        correct_labels=["A", "C"],
        all_option_labels=["A", "B", "C", "D"],
        selected_labels=["B", "D"],  # 2 wrong -> fraction = -2/2 = -1.0
        points=2.0,
        grading_config={"allow_negative": False},
    )
    assert res.score == 0.0


def test_grade_multiple_choice_custom_penalty_ratio():
    # penalty_ratio = 0.5: each wrong choice only penalizes half of 1/k
    # k = 2, correct: A, C. Selected: A, B.
    # fraction = (1 - 0.5 * 1) / 2 = 0.5 / 2 = 0.25 * points (2.0) = 0.5
    res = grade_multiple_choice(
        correct_labels=["A", "C"],
        all_option_labels=["A", "B", "C", "D"],
        selected_labels=["A", "B"],
        points=2.0,
        grading_config={"penalty_ratio": 0.5},
    )
    assert res.score == 0.5


# ---------------------------------------------------------------------------
# FILL_IN_BLANK Tests
# ---------------------------------------------------------------------------


def test_grade_fill_in_blank_exact_and_case_insensitive():
    res = grade_fill_in_blank(
        accepted_answers=["HTTP", "Hypertext Transfer Protocol"],
        student_text="http",
        points=1.0,
    )
    assert res.is_correct is True
    assert res.score == 1.0


def test_grade_fill_in_blank_pipe_separated_aliases():
    res = grade_fill_in_blank(
        accepted_answers="O(log n) | O(logn) | logarithmic",
        student_text="  O(logn)  ",
        points=2.0,
    )
    assert res.is_correct is True
    assert res.score == 2.0


def test_grade_fill_in_blank_case_sensitive():
    # When case_sensitive=True, "tcp" should not match "TCP"
    res_fail = grade_fill_in_blank(
        accepted_answers=["TCP"],
        student_text="tcp",
        points=1.0,
        grading_config={"case_sensitive": True},
    )
    assert res_fail.is_correct is False
    assert res_fail.score == 0.0

    res_ok = grade_fill_in_blank(
        accepted_answers=["TCP"],
        student_text="TCP",
        points=1.0,
        grading_config={"case_sensitive": True},
    )
    assert res_ok.is_correct is True
    assert res_ok.score == 1.0


# ---------------------------------------------------------------------------
# Router Convenience Test
# ---------------------------------------------------------------------------


def test_grade_question_submission_router():
    options = [
        {"option_label": "A", "is_correct": True},
        {"option_label": "B", "is_correct": False},
        {"option_label": "C", "is_correct": True},
    ]

    res = grade_question_submission(
        question_type="MULTIPLE_CHOICE",
        points=2.0,
        options_data=options,
        student_selected_labels=["A", "C"],
        student_text=None,
    )
    assert res.is_correct is True
    assert res.score == 2.0
