"""Excel Template Contract, Generator, and Strict Validator for Quizzes (AI-148, AI-152)."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

from app.schemas.quiz import (
    QuizOptionCreate,
    QuizQuestionCreate,
)

EXCEL_HEADERS = [
    "Question Type",
    "Question",
    "Option A",
    "Option B",
    "Option C",
    "Option D",
    "Option E",
    "Option F",
    "Correct Answer(s)",
    "Points",
    "Difficulty",
    "Explanation",
    "Rubric",
    "Source Reference",
]

QUESTION_TYPES_LIST = ["SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK"]
BLOOM_LEVELS_LIST = ["REMEMBER", "UNDERSTAND", "APPLY"]
OPTION_COLUMNS = ["Option A", "Option B", "Option C", "Option D", "Option E", "Option F"]
OPTION_KEY_MAP = {
    "Option A": "A",
    "Option B": "B",
    "Option C": "C",
    "Option D": "D",
    "Option E": "E",
    "Option F": "F",
}
MAX_QUIZ_EXCEL_SIZE_BYTES = 10 * 1024 * 1024
MAX_QUIZ_EXCEL_QUESTIONS = 1000
MAX_QUIZ_EXCEL_POINTS_WITHOUT_WARNING = 10.0

# Key used to persist FILL_IN_BLANK accepted answers in ``QuizQuestion.grading_config``.
# Shared by the importer, the exporter, and the grading service so the round trip stays stable.
FILL_IN_BLANK_ANSWERS_KEY = "correct_answers"


@dataclass
class ExcelRowError:
    row_index: int
    column: str | None
    message: str


@dataclass
class ExcelRowWarning:
    """Non-blocking notice: the row is imported, but the teacher should look at it."""

    row_index: int
    column: str | None
    message: str


@dataclass
class ExcelParseResult:
    is_valid: bool
    errors: list[ExcelRowError] = field(default_factory=list)
    questions: list[QuizQuestionCreate] = field(default_factory=list)
    warnings: list[ExcelRowWarning] = field(default_factory=list)


def _safe_str(val: Any) -> str:
    """Safely convert cell value to stripped string."""
    if val is None:
        return ""
    # Strip trailing floats like 1.0 if imported as number
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val).strip()


def validate_and_parse_quiz_excel(file_bytes_or_stream: bytes | io.BytesIO) -> ExcelParseResult:
    """Validate and parse a quiz Excel file according to the AI-148 contract.

    Returns:
        ExcelParseResult containing validation errors or parsed QuizQuestionCreate objects.
    """
    errors: list[ExcelRowError] = []
    warnings: list[ExcelRowWarning] = []
    questions: list[QuizQuestionCreate] = []
    seen_questions: dict[str, int] = {}

    if isinstance(file_bytes_or_stream, bytes):
        if len(file_bytes_or_stream) > MAX_QUIZ_EXCEL_SIZE_BYTES:
            return ExcelParseResult(
                is_valid=False,
                errors=[ExcelRowError(0, None, "Excel file exceeds the 10 MB upload limit.")],
            )
    else:
        file_bytes_or_stream.seek(0, io.SEEK_END)
        if file_bytes_or_stream.tell() > MAX_QUIZ_EXCEL_SIZE_BYTES:
            return ExcelParseResult(
                is_valid=False,
                errors=[ExcelRowError(0, None, "Excel file exceeds the 10 MB upload limit.")],
            )
        file_bytes_or_stream.seek(0)

    try:
        stream = (
            file_bytes_or_stream
            if isinstance(file_bytes_or_stream, io.BytesIO)
            else io.BytesIO(file_bytes_or_stream)
        )
        wb = openpyxl.load_workbook(stream, data_only=True)
    except Exception as exc:  # noqa: BLE001
        return ExcelParseResult(
            is_valid=False,
            errors=[ExcelRowError(row_index=0, column=None, message=f"Corrupt or invalid Excel file: {exc}")],
        )

    # Use first sheet or sheet named 'Questions' / 'Quiz'
    sheet = wb.active
    if "Questions" in wb.sheetnames:
        sheet = wb["Questions"]
    elif "Quiz" in wb.sheetnames:
        sheet = wb["Quiz"]

    if sheet is None or sheet.max_row < 1:
        return ExcelParseResult(
            is_valid=False,
            errors=[ExcelRowError(row_index=1, column=None, message="The worksheet is completely empty.")],
        )

    if sheet.max_row - 1 > MAX_QUIZ_EXCEL_QUESTIONS:
        return ExcelParseResult(
            is_valid=False,
            errors=[
                ExcelRowError(
                    MAX_QUIZ_EXCEL_QUESTIONS + 2,
                    "Question",
                    "A quiz may contain at most 1,000 question rows.",
                )
            ],
        )

    # Validate Header
    header_row = [cell.value for cell in sheet[1]]
    header_map: dict[str, int] = {}
    normalized_expected = [h.lower() for h in EXCEL_HEADERS]

    for idx, cell_val in enumerate(header_row):
        if cell_val is not None:
            clean_name = str(cell_val).strip()
            if clean_name.lower() in normalized_expected:
                # Find matching standard header name
                for standard_name in EXCEL_HEADERS:
                    if standard_name.lower() == clean_name.lower():
                        header_map[standard_name] = idx
                        break

    missing_headers = [h for h in EXCEL_HEADERS if h not in header_map]
    if missing_headers:
        errors.append(
            ExcelRowError(
                row_index=1,
                column=None,
                message=f"Missing required columns in header: {', '.join(missing_headers)}",
            )
        )
        return ExcelParseResult(is_valid=False, errors=errors)

    # Process Rows
    order_counter = 0
    for row_idx in range(2, sheet.max_row + 1):
        row_cells = sheet[row_idx]

        def get_val(col_name: str, cells=row_cells) -> str:
            col_idx = header_map[col_name]
            if col_idx < len(cells):
                return _safe_str(cells[col_idx].value)
            return ""

        q_type_raw = get_val("Question Type").upper()
        question_text = get_val("Question")

        # Skip completely empty row
        if not q_type_raw and not question_text:
            all_empty = all(not get_val(h) for h in EXCEL_HEADERS)
            if all_empty:
                continue

        row_has_errors = False

        # 1. Question Type validation
        if not q_type_raw:
            errors.append(ExcelRowError(row_idx, "Question Type", "Question Type must not be empty."))
            row_has_errors = True
        elif q_type_raw not in QUESTION_TYPES_LIST:
            errors.append(
                ExcelRowError(
                    row_idx,
                    "Question Type",
                    f"Invalid Question Type '{q_type_raw}'. Allowed: {', '.join(QUESTION_TYPES_LIST)}",
                )
            )
            row_has_errors = True

        # 2. Question Prompt validation
        if not question_text:
            errors.append(ExcelRowError(row_idx, "Question", "Question content must not be empty."))
            row_has_errors = True

        # 3. Difficulty validation (Bloom level)
        diff_raw = get_val("Difficulty").upper() or "REMEMBER"
        if diff_raw not in BLOOM_LEVELS_LIST:
            errors.append(
                ExcelRowError(
                    row_idx,
                    "Difficulty",
                    f"Invalid Difficulty '{diff_raw}'. Allowed Bloom levels: {', '.join(BLOOM_LEVELS_LIST)}",
                )
            )
            row_has_errors = True

        # 4. Points validation
        points_raw = get_val("Points")
        points = 1.0
        if points_raw:
            try:
                points = float(points_raw)
                if points <= 0:
                    errors.append(ExcelRowError(row_idx, "Points", "Points must be greater than 0."))
                    row_has_errors = True
            except ValueError:
                errors.append(ExcelRowError(row_idx, "Points", f"Invalid numeric Points value '{points_raw}'."))
                row_has_errors = True

        # Extract Options A through F
        present_options: list[tuple[str, str]] = []  # (label, text)
        saw_empty_option = False

        for opt_col in OPTION_COLUMNS:
            label = OPTION_KEY_MAP[opt_col]
            text_val = get_val(opt_col)
            if text_val:
                if saw_empty_option:
                    errors.append(
                        ExcelRowError(
                            row_idx,
                            opt_col,
                            f"Non-sequential option {label} detected. Previous option was left blank.",
                        )
                    )
                    row_has_errors = True
                present_options.append((label, text_val))
            else:
                saw_empty_option = True

        seen_options: set[str] = set()
        for label, text_val in present_options:
            normalized_option = " ".join(text_val.casefold().split())
            if normalized_option in seen_options:
                errors.append(
                    ExcelRowError(row_idx, f"Option {label}", "Duplicate option text in this question.")
                )
                row_has_errors = True
            seen_options.add(normalized_option)

        correct_raw = get_val("Correct Answer(s)")
        explanation = get_val("Explanation") or None
        rubric = get_val("Rubric") or None
        source_ref = get_val("Source Reference") or None

        # 5. Type-specific validations
        if q_type_raw in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            if len(present_options) < 2:
                errors.append(
                    ExcelRowError(
                        row_idx,
                        "Option A/B",
                        f"{q_type_raw} requires at least Option A and Option B.",
                    )
                )
                row_has_errors = True

            if not correct_raw:
                errors.append(
                    ExcelRowError(
                        row_idx,
                        "Correct Answer(s)",
                        f"{q_type_raw} requires specifying correct answer(s) (e.g. 'A' or 'A, C').",
                    )
                )
                row_has_errors = True
            else:
                # Split answers by comma or semicolon
                parsed_answers = [
                    a.strip().upper() for a in re.split(r"[,;]", correct_raw) if a.strip()
                ]
                present_labels = [lbl for lbl, _ in present_options]

                # Check if answers match existing options
                for ans in parsed_answers:
                    if ans not in present_labels:
                        errors.append(
                            ExcelRowError(
                                row_idx,
                                "Correct Answer(s)",
                                f"Answer '{ans}' does not correspond to any filled option ({', '.join(present_labels)}).",
                            )
                        )
                        row_has_errors = True

                if q_type_raw == "SINGLE_CHOICE":
                    if len(parsed_answers) != 1:
                        errors.append(
                            ExcelRowError(
                                row_idx,
                                "Correct Answer(s)",
                                f"SINGLE_CHOICE must have exactly 1 correct answer, found: {', '.join(parsed_answers)}",
                            )
                        )
                        row_has_errors = True

                elif q_type_raw == "MULTIPLE_CHOICE" and len(parsed_answers) < 1:
                    errors.append(
                        ExcelRowError(
                            row_idx,
                            "Correct Answer(s)",
                            "MULTIPLE_CHOICE must have at least 1 correct answer.",
                        )
                    )
                    row_has_errors = True

        elif q_type_raw == "FILL_IN_BLANK":
            if present_options:
                errors.append(
                    ExcelRowError(
                        row_idx,
                        "Option A",
                        "FILL_IN_BLANK questions must leave Option A through Option F blank.",
                    )
                )
                row_has_errors = True

            if not correct_raw and not rubric:
                errors.append(
                    ExcelRowError(
                        row_idx,
                        "Correct Answer(s)",
                        "FILL_IN_BLANK question requires a correct answer or rubric definition.",
                    )
                )
                row_has_errors = True

        # Build Question DTO if row has no errors
        if not row_has_errors:
            # Non-blocking warnings: the row is imported, the teacher is nudged to review it.
            if points > MAX_QUIZ_EXCEL_POINTS_WITHOUT_WARNING:
                warnings.append(
                    ExcelRowWarning(
                        row_idx,
                        "Points",
                        f"Unusually high Points value '{points:g}'; confirm this is intended.",
                    )
                )

            question_key = " ".join(question_text.casefold().split())
            first_row = seen_questions.get(question_key)
            if first_row is not None:
                warnings.append(
                    ExcelRowWarning(
                        row_idx,
                        "Question",
                        f"Duplicate question text also appears on row {first_row}.",
                    )
                )
            else:
                seen_questions[question_key] = row_idx

            parsed_correct = [
                a.strip().upper() for a in re.split(r"[,;]", correct_raw) if a.strip()
            ]

            options_dtos: list[QuizOptionCreate] = []
            for opt_idx, (lbl, txt) in enumerate(present_options):
                is_corr = lbl in parsed_correct
                options_dtos.append(
                    QuizOptionCreate(
                        option_label=lbl,  # type: ignore[arg-type]
                        option_text=txt,
                        is_correct=is_corr,
                        order_index=opt_idx,
                    )
                )

            # For fill-in-the-blank, correct_answers are split by pipe or comma
            fill_answers = []
            if q_type_raw == "FILL_IN_BLANK" and correct_raw:
                fill_answers = [a.strip() for a in re.split(r"[|\n]", correct_raw) if a.strip()]

            q_dto = QuizQuestionCreate(
                question_type=q_type_raw,  # type: ignore[arg-type]
                question=question_text,
                points=points,
                difficulty=diff_raw,  # type: ignore[arg-type]
                order_index=order_counter,
                explanation=explanation,
                rubric=rubric,
                source_reference=source_ref,
                options=options_dtos,
                correct_answers=fill_answers,
            )
            questions.append(q_dto)
            order_counter += 1

    if not questions and not errors:
        errors.append(ExcelRowError(2, "Question", "At least one question is required."))

    # Parsed rows are always returned so a preview can show valid questions next to errors.
    # Import remains gated on ``is_valid``.
    return ExcelParseResult(
        is_valid=(len(errors) == 0),
        errors=errors,
        questions=questions,
        warnings=warnings,
    )


def generate_quiz_template_workbook() -> openpyxl.Workbook:
    """Generate a formatted Excel template workbook with validation dropdowns and sample rows."""
    wb = openpyxl.Workbook()

    # 1. Sheet 'Questions'
    ws = wb.active
    ws.title = "Questions"

    # Styling definitions
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # Write headers
    for col_idx, header in enumerate(EXCEL_HEADERS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border

    # Sample rows demonstrating each question type
    sample_rows = [
        [
            "SINGLE_CHOICE",
            "What does CPU stand for?",
            "Central Processing Unit",
            "Central Program Unit",
            "Computer Processing Utility",
            "Core Processing Unit",
            "",
            "",
            "A",
            1.0,
            "REMEMBER",
            "The Central Processing Unit is the primary component that executes instructions.",
            "",
            "Computer Architecture Chapter 1",
        ],
        [
            "MULTIPLE_CHOICE",
            "Which of the following are valid HTTP methods for idempotent requests?",
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "CONNECT",
            "",
            "A, C, D",
            2.0,
            "UNDERSTAND",
            "GET, PUT, and DELETE are idempotent according to RFC 9110 specification.",
            "Penalty applies for choosing non-idempotent methods like POST.",
            "Web Protocols Lecture 3, Slide 14",
        ],
        [
            "FILL_IN_BLANK",
            "Which protocol provides secure encrypted transport over port 443?",
            "",
            "",
            "",
            "",
            "",
            "",
            "HTTPS | HTTP over TLS",
            1.0,
            "APPLY",
            "HTTPS uses TLS/SSL to provide encrypted communications over TCP port 443.",
            "Accept HTTPS or HTTP over TLS (case-insensitive)",
            "Network Security Handbook, Section 4.2",
        ],
    ]

    for row_idx, row_data in enumerate(sample_rows, start=2):
        for col_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            cell.font = Font(name="Calibri", size=10)
            if col_idx in (1, 9, 10, 11):
                cell.alignment = center_align
            else:
                cell.alignment = left_align

    # Add Data Validations for Question Type & Difficulty
    type_dv = DataValidation(
        type="list",
        formula1=f'"{",".join(QUESTION_TYPES_LIST)}"',
        allow_blank=True,
    )
    ws.add_data_validation(type_dv)
    type_dv.add("A2:A1000")

    diff_dv = DataValidation(
        type="list",
        formula1=f'"{",".join(BLOOM_LEVELS_LIST)}"',
        allow_blank=True,
    )
    ws.add_data_validation(diff_dv)
    diff_dv.add("K2:K1000")

    # Column widths
    col_widths = {
        "A": 18,  # Question Type
        "B": 40,  # Question
        "C": 25,  # Option A
        "D": 25,  # Option B
        "E": 25,  # Option C
        "F": 25,  # Option D
        "G": 20,  # Option E
        "H": 20,  # Option F
        "I": 18,  # Correct Answer(s)
        "J": 10,  # Points
        "K": 16,  # Difficulty
        "L": 35,  # Explanation
        "M": 30,  # Rubric
        "N": 30,  # Source Reference
    }
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    # 2. Sheet 'Instructions'
    ws_guide = wb.create_sheet(title="Instructions")
    ws_guide.column_dimensions["A"].width = 25
    ws_guide.column_dimensions["B"].width = 80

    instructions = [
        ("Column Name", "Detailed Requirement & Contract Rule"),
        ("Question Type", "Required. Choose one: SINGLE_CHOICE, MULTIPLE_CHOICE, or FILL_IN_BLANK."),
        ("Question", "Required. The main prompt or question text."),
        ("Option A & Option B", "Required for SINGLE_CHOICE and MULTIPLE_CHOICE. Must be left blank for FILL_IN_BLANK."),
        ("Option C to Option F", "Optional. Sequential only (cannot skip Option C to write Option D). Must be blank for FILL_IN_BLANK."),
        ("Correct Answer(s)", "For SINGLE_CHOICE: Single uppercase letter (e.g. A). For MULTIPLE_CHOICE: Letters separated by comma (e.g. A, C). For FILL_IN_BLANK: Accepted answer string, multiple equivalents separated by pipe (e.g. HTTPS | HTTP over TLS)."),
        ("Points", "Positive number. Default is 1.0 if left blank."),
        ("Difficulty", "Bloom Levels: REMEMBER (recall facts), UNDERSTAND (comprehend concepts), or APPLY (use knowledge in situations)."),
        ("Explanation", "Optional. Detailed solution shown to students in review mode after submission."),
        ("Rubric", "Optional. Grading rubric or instructions for manual review / agent grading."),
        ("Source Reference", "Optional. Document name, chapter, slide, or page number where the answer is found."),
    ]

    for r_idx, (col_n, desc) in enumerate(instructions, start=1):
        cell_a = ws_guide.cell(row=r_idx, column=1, value=col_n)
        cell_b = ws_guide.cell(row=r_idx, column=2, value=desc)
        cell_a.border = thin_border
        cell_b.border = thin_border
        if r_idx == 1:
            cell_a.fill = header_fill
            cell_a.font = header_font
            cell_b.fill = header_fill
            cell_b.font = header_font
        else:
            cell_a.font = Font(name="Calibri", size=10, bold=True)
            cell_b.font = Font(name="Calibri", size=10)

    return wb


def export_quiz_to_workbook(questions: list[Any]) -> openpyxl.Workbook:
    """Export question objects/DTOs to an openpyxl Workbook conforming to the AI-148 template contract.

    Guarantees No-Schema-Drift: Re-importing the returned workbook via
    ``validate_and_parse_quiz_excel`` recreates identical question specifications.
    """
    wb = generate_quiz_template_workbook()
    ws = wb["Questions"]

    # Clear sample rows (keep header row 1)
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)

    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    for row_idx, q in enumerate(questions, start=2):
        # Support both Pydantic models and SQLAlchemy model instances
        q_type = getattr(q, "question_type", "")
        q_text = getattr(q, "question", "")
        points = float(getattr(q, "points", 1.0))
        diff = getattr(q, "difficulty", "REMEMBER")
        explanation = getattr(q, "explanation", "") or ""
        rubric = getattr(q, "rubric", "") or ""
        source_ref = getattr(q, "source_reference", "") or ""

        # Options handling
        options = getattr(q, "options", []) or []
        opt_dict: dict[str, str] = {}
        correct_labels: list[str] = []

        for opt in options:
            label = str(getattr(opt, "option_label", "")).upper()
            text_val = getattr(opt, "option_text", "")
            is_corr = bool(getattr(opt, "is_correct", False))
            opt_dict[label] = text_val
            if is_corr:
                correct_labels.append(label)

        if q_type == "FILL_IN_BLANK":
            correct_ans_str = ""
            raw_correct = getattr(q, "correct_answers", None)
            if not raw_correct:
                # DB-backed questions keep accepted answers in ``grading_config``.
                config = getattr(q, "grading_config", None) or {}
                raw_correct = config.get(FILL_IN_BLANK_ANSWERS_KEY)
            if raw_correct:
                correct_ans_str = " | ".join(raw_correct)
            elif rubric:
                correct_ans_str = rubric
        else:
            correct_ans_str = ", ".join(sorted(correct_labels))

        row_data = [
            q_type,
            q_text,
            opt_dict.get("A", ""),
            opt_dict.get("B", ""),
            opt_dict.get("C", ""),
            opt_dict.get("D", ""),
            opt_dict.get("E", ""),
            opt_dict.get("F", ""),
            correct_ans_str,
            points,
            diff,
            explanation,
            rubric,
            source_ref,
        ]

        for col_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            cell.font = Font(name="Calibri", size=10)
            if col_idx in (1, 9, 10, 11):
                cell.alignment = center_align
            else:
                cell.alignment = left_align

    return wb
