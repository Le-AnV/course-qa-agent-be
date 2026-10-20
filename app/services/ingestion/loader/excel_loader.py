"""Excel loader - converts syllabus and general workbook data into Markdown.

The loader uses **section markers in column A** (integer labels 1-12) to
locate each section dynamically, so it works even when rows shift between
different syllabus files.  Sheet names are matched flexibly (e.g.
``L2_PY_PE_Syllabus`` is found by searching for "syllabus" in the name).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

# Helpers


def _clean(value) -> str:
    """Convert a cell value to a clean string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    # Collapse stray tabs left by some Excel exports
    text = text.replace("\t", " ")
    return text


def _format_percentage(value) -> str:
    """Format a decimal fraction as a percentage string."""
    if value is None:
        return ""
    try:
        return f"{float(value) * 100:.0f}%"
    except (ValueError, TypeError):
        return _clean(value)


def _format_duration(value) -> str:
    """Format a duration value with 'min' suffix."""
    if value is None:
        return ""
    return f"{value} min"


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Build a Markdown table from headers and row data."""
    if not rows:
        return ""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        safe = [c.replace("\n", "; ").replace("|", "\\|") for c in row]
        lines.append("| " + " | ".join(safe) + " |")
    return "\n".join(lines)


def _find_sheet(workbook, keyword: str) -> Worksheet | None:
    """Find a worksheet whose name contains *keyword* (case-insensitive).

    Returns ``None`` when no match is found.
    """
    kw = keyword.lower()
    for name in workbook.sheetnames:
        if kw in name.lower():
            return workbook[name]
    return None


def _find_section_row(ws: Worksheet, marker: int) -> int | None:
    """Find the row where column A equals *marker* (int label 1-12)."""
    for row in range(1, ws.max_row + 1):
        val = ws[f"A{row}"].value
        if val is not None:
            try:
                if int(val) == marker:
                    return row
            except (ValueError, TypeError):
                continue
    return None


def _collect_rows_until_next_marker(ws: Worksheet, start_row: int) -> list[int]:
    """Return row numbers from *start_row* until the next non-empty col-A
    value (exclusive) or end of sheet."""
    rows = [start_row]
    for r in range(start_row + 1, ws.max_row + 1):
        val = ws[f"A{r}"].value
        if val is not None:
            try:
                int(val)
                break  # next numbered section
            except (ValueError, TypeError):
                break  # non-numeric marker (e.g. "Technical group" list)
        rows.append(r)
    return rows


# Section parsers


def _parse_cover(ws: Worksheet) -> str:
    """Parse the Cover sheet."""
    # Title may span B11-B14 depending on file
    title_parts = []
    for r in [11, 12, 13, 14, 15]:
        v = _clean(ws[f"B{r}"].value)
        if v:
            title_parts.append(v)
    title = " - ".join(title_parts) if title_parts else "Syllabus"

    document_code = _clean(ws["I17"].value)
    version = _clean(ws["I18"].value)
    effective_date = _clean(ws["I19"].value)

    return f"""# {title}

## Document Information

- Document Code: {document_code}
- Version: {version}
- Effective Date: {effective_date}
"""


def _parse_syllabus(ws: Worksheet) -> str:
    """Parse the Syllabus sheet by locating sections via column-A markers."""
    parts: list[str] = []

    # Helper to safely read cell at a marker row
    def _val(marker: int, col: str = "C") -> str:
        r = _find_section_row(ws, marker)
        return _clean(ws[f"{col}{r}"].value) if r else ""

    # Section 1-5: Course Info
    parts.extend(
        [
            "## Course Information",
            "",
            f"- Technical Group: {_val(1)}",
            f"- Topic Name: {_val(2)}",
            f"- Topic Code: {_val(3)}",
            f"- Version: {_val(4)}",
            f"- Training Audience: {_val(5)}",
        ]
    )

    # Section 6: Objectives & Outcomes
    obj_row = _find_section_row(ws, 6)
    if obj_row:
        objectives = _clean(ws[f"C{obj_row}"].value)
        outcome_rows = _collect_rows_until_next_marker(ws, obj_row)
        outcome_items = []
        for r in outcome_rows[1:]:
            name = _clean(ws[f"C{r}"].value)
            code = _clean(ws[f"D{r}"].value)
            desc = _clean(ws[f"E{r}"].value)
            if name and name.lower() != "name":
                row_parts = [name]
                if code:
                    row_parts.append(f"({code})")
                if desc:
                    row_parts.append(f"- {desc}")
                outcome_items.append("- " + " ".join(row_parts))

        parts.extend(["", "## Course Objectives", "", objectives])
        if outcome_items:
            parts.extend(["", "### Expected Outcomes", ""] + outcome_items)

    # Section 7: Topic Outline
    outline_row = _find_section_row(ws, 7)
    if outline_row:
        outline = _clean(ws[f"C{outline_row}"].value)
        parts.extend(["", "## Topic Outline", "", outline])

    # Section 8: Time Allocation
    time_row = _find_section_row(ws, 8)
    if time_row:
        time_rows = _collect_rows_until_next_marker(ws, time_row)
        table_data = []
        for r in time_rows:
            category = _clean(ws[f"C{r}"].value)
            percentage = ws[f"D{r}"].value
            description = _clean(ws[f"E{r}"].value)
            notes = _clean(ws[f"F{r}"].value)
            if category:
                table_data.append(
                    [
                        category,
                        _format_percentage(percentage),
                        description,
                        notes,
                    ]
                )
        table = _markdown_table(["Type", "Percentage", "Description", "Notes"], table_data)
        parts.extend(["", "## Time Allocation", "", table])

    # Section 9: Training Materials
    # Some files lack marker 10, so assessment rows sit inside the section-9
    # block.  We detect them by checking for well-known assessment keywords.
    _ASSESSMENT_KEYWORDS = {
        "quiz",
        "quizzes",
        "assignments",
        "assignment",
        "final theory test",
        "final practice test",
        "final pracice test",
        "presentation",
        "pass criteria",
        "online video",
    }

    mat_row = _find_section_row(ws, 9)
    assess_row = _find_section_row(ws, 10)

    if mat_row:
        mat_rows = _collect_rows_until_next_marker(ws, mat_row)
        material_lines: list[str] = []
        embedded_assess: list[list[str]] = []
        embedded_pass = ""

        for r in mat_rows:
            label = _clean(ws[f"C{r}"].value)
            detail = _clean(ws[f"D{r}"].value)
            weight = _clean(ws[f"E{r}"].value)
            notes = _clean(ws[f"F{r}"].value)
            if not label:
                continue

            # Decide whether this row is assessment or material
            if not assess_row and label.lower() in _ASSESSMENT_KEYWORDS:
                if label == "Pass Criteria":
                    embedded_pass = weight if weight else notes
                else:
                    embedded_assess.append([label, detail, weight, notes])
            else:
                if detail:
                    material_lines.append(f"- {label}: {detail}")
                else:
                    material_lines.append(f"- {label}")

        parts.extend(["", "## Training Materials & Environments", ""] + material_lines)

        # Flush any embedded assessment rows
        if embedded_assess:
            table = _markdown_table(["Assessment", "Count", "Weight", "Notes"], embedded_assess)
            parts.extend(["", "## Assessment Scheme", "", table])
            if embedded_pass:
                parts.extend(["", "### Pass Criteria", "", embedded_pass])

    # Section 10: Assessment Scheme (explicit marker)
    if assess_row:
        _parse_assessment_section(ws, assess_row, parts)

    # Section 11: Delivery Principles
    deliv_row = _find_section_row(ws, 11)
    if deliv_row:
        deliv_rows = _collect_rows_until_next_marker(ws, deliv_row)
        table_data = []
        for r in deliv_rows:
            role = _clean(ws[f"C{r}"].value)
            requirement = _clean(ws[f"D{r}"].value)
            if role:
                table_data.append([role, requirement])
        table = _markdown_table(["Role", "Requirement"], table_data)
        parts.extend(["", "## Training Delivery Principles", "", table])

    return "\n".join(parts)


def _parse_assessment_section(ws: Worksheet, assess_row: int, parts: list[str]) -> None:
    """Parse the Assessment Scheme section and append to *parts*."""
    assess_rows = _collect_rows_until_next_marker(ws, assess_row)
    table_data = []
    pass_criteria = ""
    for r in assess_rows:
        assessment = _clean(ws[f"C{r}"].value)
        count = _clean(ws[f"D{r}"].value)
        weight = _clean(ws[f"E{r}"].value)
        notes = _clean(ws[f"F{r}"].value)
        if assessment == "Pass Criteria":
            pass_criteria = weight if weight else notes
            continue
        if assessment:
            table_data.append([assessment, count, weight, notes])

    table = _markdown_table(["Assessment", "Count", "Weight", "Notes"], table_data)
    parts.extend(["", "## Assessment Scheme", "", table])
    if pass_criteria:
        parts.extend(["", "### Pass Criteria", "", pass_criteria])


def _parse_schedule(ws: Worksheet) -> str:
    """Parse the ScheduleDetail sheet - reads ALL data rows dynamically."""
    items = []
    for row in range(3, ws.max_row + 1):
        content = _clean(ws[f"D{row}"].value)
        if not content:
            continue
        items.append(
            {
                "unit": ws[f"A{row}"].value,
                "chapter": _clean(ws[f"B{row}"].value),
                "session": ws[f"C{row}"].value,
                "content": content,
                "objectives": _clean(ws[f"E{row}"].value),
                "delivery": _clean(ws[f"F{row}"].value),
                "duration": ws[f"G{row}"].value,
                "format": _clean(ws[f"H{row}"].value),
                "notes": _clean(ws[f"I{row}"].value),
            }
        )

    if not items:
        return ""

    sections: list[str] = ["# Training Schedule"]
    current_chapter: str | None = None
    current_session = None
    table_rows: list[list[str]] = []

    def flush_table():
        nonlocal table_rows
        if not table_rows:
            return
        sections.append(
            _markdown_table(
                [
                    "Content",
                    "Learning Objectives",
                    "Delivery Type",
                    "Duration",
                    "Training Format",
                    "Notes",
                ],
                table_rows,
            )
        )
        table_rows = []

    for item in items:
        chapter = item["chapter"]
        if chapter and chapter != current_chapter:
            flush_table()
            current_chapter = chapter
            current_session = None
            sections.extend(["", f"## {chapter}"])

        session = item["session"]
        if session is not None and session != current_session:
            flush_table()
            current_session = session
            sections.extend(["", f"### Session {session}"])

        table_rows.append(
            [
                item["content"],
                item["objectives"],
                item["delivery"],
                _format_duration(item["duration"]),
                item["format"],
                item["notes"],
            ]
        )

    flush_table()
    return "\n".join(sections)


def _parse_change_history(ws: Worksheet) -> str:
    """Parse the Author and Record of Changes sheet."""
    # Author table
    author_rows = []
    for row in range(3, 6):
        role = _clean(ws[f"B{row}"].value)
        name = _clean(ws[f"C{row}"].value)
        account = _clean(ws[f"D{row}"].value)
        unit = _clean(ws[f"E{row}"].value)
        if role:
            author_rows.append([role, name, account, unit])

    # Change records - find "Date" header row dynamically
    header_row = None
    for r in range(6, ws.max_row + 1):
        if _clean(ws[f"B{r}"].value) == "Date":
            header_row = r
            break

    changes = []
    if header_row:
        for row in range(header_row + 1, ws.max_row + 1):
            date = _clean(ws[f"B{row}"].value)
            change = _clean(ws[f"C{row}"].value)
            action = _clean(ws[f"D{row}"].value)
            contents = _clean(ws[f"E{row}"].value)
            version = _clean(ws[f"F{row}"].value)
            if not any([date, change, action, contents, version]):
                continue
            changes.append([date, change, action, contents, version])

    parts = [
        "## Document History",
        "",
        "### Authorship",
        "",
        _markdown_table(["Role", "Name", "Account", "Unit"], author_rows),
        "",
        "### Record of Changes",
        "",
        _markdown_table(["Date", "Changes", "Action", "Contents", "Version"], changes),
    ]

    return "\n".join(parts)


def _parse_generic_sheet(ws: Worksheet) -> str:
    """Convert one ordinary worksheet to row-located Markdown."""
    rows: list[tuple[int, list[str]]] = []
    for row_number, values in enumerate(ws.iter_rows(values_only=True), start=1):
        text_values = [
            _clean(value).replace("\r", " ").replace("\n", " ").strip() for value in values
        ]
        if any(text_values):
            rows.append((row_number, text_values))

    if len(rows) < 2:
        return ""

    sheet_name = _clean(ws.title).replace("\r", " ").replace("\n", " ").strip()
    headers = rows[0][1]
    sheet_lines = [f"# {sheet_name}"]
    for row_number, values in rows[1:]:
        fields = []
        for index, value in enumerate(values):
            if not value:
                continue
            header = headers[index] if index < len(headers) else ""
            label = header or f"Column {index + 1}"
            fields.append(f"{label}: {value}")
        if fields:
            sheet_lines.extend(["", f"## {sheet_name}—Row {row_number}", "", *fields])

    return "\n".join(sheet_lines) if len(sheet_lines) > 1 else ""


def _has_cover_content(ws: Worksheet) -> bool:
    cells = [*(f"B{row}" for row in range(11, 16)), "I17", "I18", "I19"]
    return any(_clean(ws[cell].value) for cell in cells)


def _has_syllabus_content(ws: Worksheet) -> bool:
    has_section_marker = any(_find_section_row(ws, marker) for marker in range(1, 12))
    has_section_value = any(
        _clean(ws.cell(row=row, column=column).value)
        for row in range(1, ws.max_row + 1)
        for column in range(3, 7)
    )
    return has_section_marker and has_section_value


def _has_change_history_content(ws: Worksheet) -> bool:
    return any(
        _clean(ws.cell(row=row, column=column).value)
        for row in range(3, ws.max_row + 1)
        for column in range(2, 7)
    )


# Public API


def excel_loader(file_path: str | Path) -> dict[str, Any]:
    """Load a syllabus or ordinary Excel workbook and convert it to Markdown.

    Handles variations in sheet naming conventions (e.g. ``Syllabus`` vs
    ``L2_PY_PE_Syllabus``) and dynamic row layouts across different syllabi.

    Args:
        file_path: Path to the ``.xlsx`` syllabus file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the Excel file is empty or contains no readable syllabus text.

    Returns:
        Dict with keys ``markdown`` (str), ``metadata`` (dict), and ``images`` (dict).
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    workbook = load_workbook(str(path), data_only=True)

    sections: list[str] = []
    handled_sheets: set[str] = set()

    # Cover
    cover_ws = _find_sheet(workbook, "cover")
    if cover_ws and _has_cover_content(cover_ws):
        sections.append(_parse_cover(cover_ws))
        handled_sheets.add(cover_ws.title)

    # Syllabus (match "syllabus" but NOT "schedule")
    syllabus_ws = None
    for name in workbook.sheetnames:
        if "syllabus" in name.lower() and "schedule" not in name.lower():
            syllabus_ws = workbook[name]
            break
    if syllabus_ws and _has_syllabus_content(syllabus_ws):
        sections.append(_parse_syllabus(syllabus_ws))
        handled_sheets.add(syllabus_ws.title)

    # ScheduleDetail
    schedule_ws = _find_sheet(workbook, "schedule")
    if schedule_ws:
        schedule_markdown = _parse_schedule(schedule_ws)
        if schedule_markdown.strip():
            sections.append(schedule_markdown)
            handled_sheets.add(schedule_ws.title)

    # Author and Record of Changes
    for name in workbook.sheetnames:
        nl = name.lower()
        if ("author" in nl or "change" in nl or "rec" in nl) and _has_change_history_content(
            workbook[name]
        ):
            sections.append(_parse_change_history(workbook[name]))
            handled_sheets.add(name)
            break

    for worksheet in workbook.worksheets:
        if worksheet.title not in handled_sheets:
            generic_markdown = _parse_generic_sheet(worksheet)
            if generic_markdown.strip():
                sections.append(generic_markdown)

    markdown = "\n\n".join(s for s in sections if s.strip())
    workbook.close()

    if not markdown.strip():
        raise ValueError(f"Excel file '{path.name}' is empty or contains no readable text.")

    return {
        "markdown": markdown,
        "metadata": {"source": str(path), "type": "excel"},
        "images": {},
    }
