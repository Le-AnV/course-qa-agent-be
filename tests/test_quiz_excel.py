"""Unit tests for Quiz Excel template contract, generator, validator, and no-schema-drift (AI-148)."""

import io
from types import SimpleNamespace

import openpyxl

from app.schemas.quiz import QuizOptionCreate, QuizQuestionCreate
from app.services.quiz_excel import (
    EXCEL_HEADERS,
    FILL_IN_BLANK_ANSWERS_KEY,
    MAX_QUIZ_EXCEL_QUESTIONS,
    MAX_QUIZ_EXCEL_SIZE_BYTES,
    export_quiz_to_workbook,
    generate_quiz_template_workbook,
    validate_and_parse_quiz_excel,
)


def _wb_to_bytes(wb: openpyxl.Workbook) -> bytes:
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream.read()


def test_generate_template_structure():
    wb = generate_quiz_template_workbook()
    assert "Questions" in wb.sheetnames
    assert "Instructions" in wb.sheetnames

    ws = wb["Questions"]
    headers = [cell.value for cell in ws[1]]
    assert headers == EXCEL_HEADERS

    # Validate that the default template passes its own validator
    excel_bytes = _wb_to_bytes(wb)
    res = validate_and_parse_quiz_excel(excel_bytes)
    assert res.is_valid is True
    assert len(res.errors) == 0
    assert len(res.questions) == 3


def test_validate_empty_and_corrupt_file():
    # Corrupt data
    res_corrupt = validate_and_parse_quiz_excel(b"Not an excel file content")
    assert res_corrupt.is_valid is False
    assert any("Corrupt or invalid" in e.message for e in res_corrupt.errors)

    # Completely blank workbook
    empty_wb = openpyxl.Workbook()
    res_empty = validate_and_parse_quiz_excel(_wb_to_bytes(empty_wb))
    assert res_empty.is_valid is False
    assert any("Missing required columns" in e.message for e in res_empty.errors)


def test_validate_missing_header_columns():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Questions"
    # Missing "Difficulty" and "Question"
    ws.append(["Question Type", "Option A", "Option B", "Correct Answer(s)"])
    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    assert any("Missing required columns in header" in e.message for e in res.errors)


def test_validate_invalid_question_type_and_bloom_level():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    # Row 2 is first data row
    ws["A2"] = "UNKNOWN_TYPE"
    ws["K2"] = "IMPOSSIBLE_DIFFICULTY"

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    messages = [e.message for e in res.errors]
    assert any("Invalid Question Type 'UNKNOWN_TYPE'" in m for m in messages)
    assert any("Invalid Difficulty 'IMPOSSIBLE_DIFFICULTY'" in m for m in messages)


def test_validate_empty_question_and_invalid_points():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    ws["B2"] = ""  # Empty question
    ws["J2"] = "-5.0"  # Negative points

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    messages = [e.message for e in res.errors]
    assert any("Question content must not be empty" in m for m in messages)
    assert any("Points must be greater than 0" in m for m in messages)


def test_validate_non_sequential_options():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    # Option C is left empty, but Option D has text
    ws["E2"] = ""
    ws["F2"] = "Some option D text"

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    messages = [e.message for e in res.errors]
    assert any("Non-sequential option D detected" in m for m in messages)


def test_validate_single_choice_multiple_answers():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    # Single choice must not have A, B
    ws["I2"] = "A, B"

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    messages = [e.message for e in res.errors]
    assert any("SINGLE_CHOICE must have exactly 1 correct answer" in m for m in messages)


def test_validate_answer_not_in_options():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    # Only options A to D are filled in row 2, but correct answer refers to F
    ws["I2"] = "F"

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    messages = [e.message for e in res.errors]
    assert any("Answer 'F' does not correspond to any filled option" in m for m in messages)


def test_validate_fill_in_blank_with_options_forbidden():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    # Row 4 is FILL_IN_BLANK; illegally populate Option A
    ws["C4"] = "Illegal Option A"

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    messages = [e.message for e in res.errors]
    assert any("FILL_IN_BLANK questions must leave Option A through Option F blank" in m for m in messages)


def test_validate_fill_in_blank_without_answers():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    # Row 4 is FILL_IN_BLANK; clear both correct answers and rubric
    ws["I4"] = ""
    ws["M4"] = ""

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    messages = [e.message for e in res.errors]
    assert any("FILL_IN_BLANK question requires a correct answer or rubric" in m for m in messages)


def test_no_schema_drift_round_trip():
    """Verify that export -> import preserves 100% of question specifications."""
    questions = [
        QuizQuestionCreate(
            question_type="SINGLE_CHOICE",
            question="What is the port for HTTP?",
            points=1.0,
            difficulty="REMEMBER",
            order_index=0,
            explanation="HTTP uses port 80 by default.",
            source_reference="Networking 101",
            options=[
                QuizOptionCreate(option_label="A", option_text="80", is_correct=True, order_index=0),
                QuizOptionCreate(option_label="B", option_text="443", is_correct=False, order_index=1),
                QuizOptionCreate(option_label="C", option_text="22", is_correct=False, order_index=2),
            ],
        ),
        QuizQuestionCreate(
            question_type="MULTIPLE_CHOICE",
            question="Select prime numbers below 10",
            points=2.5,
            difficulty="UNDERSTAND",
            order_index=1,
            explanation="2, 3, 5, 7 are prime numbers.",
            options=[
                QuizOptionCreate(option_label="A", option_text="2", is_correct=True, order_index=0),
                QuizOptionCreate(option_label="B", option_text="3", is_correct=True, order_index=1),
                QuizOptionCreate(option_label="C", option_text="4", is_correct=False, order_index=2),
                QuizOptionCreate(option_label="D", option_text="5", is_correct=True, order_index=3),
                QuizOptionCreate(option_label="E", option_text="6", is_correct=False, order_index=4),
                QuizOptionCreate(option_label="F", option_text="7", is_correct=True, order_index=5),
            ],
        ),
        QuizQuestionCreate(
            question_type="FILL_IN_BLANK",
            question="What protocol resolves IP addresses to MAC addresses?",
            points=1.5,
            difficulty="APPLY",
            order_index=2,
            explanation="Address Resolution Protocol (ARP)",
            rubric="Accept ARP or Address Resolution Protocol",
            source_reference="Data Link Layer chapter",
            correct_answers=["ARP", "Address Resolution Protocol"],
        ),
    ]

    # Export to Excel workbook
    exported_wb = export_quiz_to_workbook(questions)
    excel_bytes = _wb_to_bytes(exported_wb)

    # Re-import via validator
    result = validate_and_parse_quiz_excel(excel_bytes)

    assert result.is_valid is True, f"Re-imported file had validation errors: {result.errors}"
    assert len(result.questions) == 3

    # Compare fields
    for orig, imported in zip(questions, result.questions):
        assert orig.question_type == imported.question_type
        assert orig.question == imported.question
        assert orig.points == imported.points
        assert orig.difficulty == imported.difficulty
        assert orig.explanation == imported.explanation
        assert orig.source_reference == imported.source_reference
        assert len(orig.options) == len(imported.options)

        for opt_orig, opt_imp in zip(orig.options, imported.options):
            assert opt_orig.option_label == opt_imp.option_label
            assert opt_orig.option_text == opt_imp.option_text
            assert opt_orig.is_correct == opt_imp.is_correct


# ---------------------------------------------------------------------------
# AI-152: import guards, warnings, and DB-backed export
# ---------------------------------------------------------------------------


def test_validate_rejects_duplicate_option_text():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    ws["D2"] = f"  {ws['C2'].value.upper()}  "  # Same text as Option A, different case/spacing

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    assert any("Duplicate option text" in e.message for e in res.errors)
    assert all(e.row_index == 2 for e in res.errors if "Duplicate option" in e.message)


def test_validate_rejects_oversized_payload():
    oversized = b"PK" + b"0" * MAX_QUIZ_EXCEL_SIZE_BYTES

    res = validate_and_parse_quiz_excel(oversized)
    assert res.is_valid is False
    assert any("10 MB" in e.message for e in res.errors)


def test_validate_rejects_too_many_question_rows():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Questions"
    ws.append(EXCEL_HEADERS)
    for _ in range(MAX_QUIZ_EXCEL_QUESTIONS + 1):
        ws.append(["x"] * len(EXCEL_HEADERS))

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    assert any("at most 1,000 question rows" in e.message for e in res.errors)


def test_validate_requires_at_least_one_question():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    ws.delete_rows(2, ws.max_row - 1)

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    assert any("At least one question is required" in e.message for e in res.errors)


def test_validate_warns_without_blocking_import():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    ws["J2"] = 15.0  # Above the warning threshold
    ws["B3"] = ws["B2"].value  # Duplicate question text of row 2

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is True
    assert len(res.questions) == 3
    messages = [w.message for w in res.warnings]
    assert any("Unusually high Points" in m for m in messages)
    assert any("Duplicate question text also appears on row 2" in m for m in messages)


def test_validate_keeps_valid_questions_next_to_errors():
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]
    ws["A2"] = "NOT_A_TYPE"  # Only row 2 is invalid

    res = validate_and_parse_quiz_excel(_wb_to_bytes(wb))
    assert res.is_valid is False
    assert len(res.questions) == 2
    assert [q.order_index for q in res.questions] == [0, 1]


def test_export_reads_fill_in_answers_from_grading_config():
    """DB-backed FILL_IN_BLANK questions keep answers in grading_config (AI-152)."""
    db_question = SimpleNamespace(
        question_type="FILL_IN_BLANK",
        question="Which protocol provides encrypted transport over port 443?",
        points=1.0,
        difficulty="APPLY",
        explanation=None,
        rubric=None,
        source_reference=None,
        options=[],
        grading_config={FILL_IN_BLANK_ANSWERS_KEY: ["HTTPS", "HTTP over TLS"]},
    )

    wb = export_quiz_to_workbook([db_question])
    assert wb["Questions"].cell(row=2, column=9).value == "HTTPS | HTTP over TLS"
