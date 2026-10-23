from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.ingestion.loader.excel_loader import excel_loader


def test_excel_loader_empty_workbook(tmp_path: Path):
    excel_path = tmp_path / "empty.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "EmptySheet"
    wb.save(str(excel_path))

    with pytest.raises(ValueError, match="empty or contains no readable text"):
        excel_loader(str(excel_path))


def test_excel_loader_file_not_found(tmp_path: Path):
    non_existent = tmp_path / "not_found.xlsx"
    with pytest.raises(FileNotFoundError, match="File not found"):
        excel_loader(str(non_existent))


def test_excel_loader_with_cover_and_syllabus(tmp_path: Path):
    excel_path = tmp_path / "course_syllabus.xlsx"
    wb = Workbook()

    # Cover sheet
    ws_cover = wb.active
    ws_cover.title = "Cover"
    ws_cover["B11"] = "Python Core Course"

    # Syllabus sheet
    ws_syl = wb.create_sheet(title="Course_Syllabus")
    # Section marker 1 in column A, value in column C
    ws_syl["A1"] = 1
    ws_syl["C1"] = "Programming Fundamentals"

    wb.save(str(excel_path))

    result = excel_loader(excel_path)

    assert result["metadata"]["type"] == "excel"
    assert result["metadata"]["source"] == str(excel_path)
    md = result["markdown"]
    assert "Python Core Course" in md
    assert "Programming Fundamentals" in md
def test_excel_loader_falls_back_to_generic_workbook_and_skips_empty_sheets(tmp_path: Path):
    excel_path = tmp_path / "lesson.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Notes"
    ws.append(["STT", "Nội dung"])
    ws.append([1, "Dòng 1"])
    ws.append([2, "Dòng 2"])
    wb.create_sheet(title="Empty")
    wb.save(str(excel_path))

    result = excel_loader(excel_path)

    assert "# Notes" in result["markdown"]
    assert "## Notes—Row 2" in result["markdown"]
    assert "STT: 1" in result["markdown"]
    assert "Nội dung: Dòng 1" in result["markdown"]
    assert "Empty" not in result["markdown"]


def test_excel_loader_keeps_generic_sheet_beside_specialized_cover(tmp_path: Path):
    excel_path = tmp_path / "mixed.xlsx"
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["B11"] = "Course"
    notes = wb.create_sheet(title="Notes")
    notes.append(["STT", "Nội dung"])
    notes.append([1, "Dòng 1"])
    wb.save(str(excel_path))

    result = excel_loader(excel_path)

    assert "# Course" in result["markdown"]
    assert "# Notes" in result["markdown"]
    assert "Nội dung: Dòng 1" in result["markdown"]


def test_excel_loader_uses_generic_parser_for_non_template_syllabus_sheet(tmp_path: Path):
    excel_path = tmp_path / "generic_syllabus.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Syllabus"
    ws.append(["STT", "Nội dung"])
    ws.append([1, "Dòng 1"])
    wb.save(str(excel_path))

    result = excel_loader(excel_path)

    assert "# Syllabus" in result["markdown"]
    assert "Nội dung: Dòng 1" in result["markdown"]
    assert "Technical Group:" not in result["markdown"]


def test_excel_loader_skips_hidden_sheets_and_filters_error_codes(tmp_path: Path):
    excel_path = tmp_path / "errors_and_hidden.xlsx"
    wb = Workbook()

    # Visible sheet with some broken formula cells
    ws_visible = wb.active
    ws_visible.title = "Visible"
    ws_visible.append(["Col1", "Col2"])
    ws_visible.append(["Valid Data", "#REF!"])
    ws_visible.append(["#VALUE!", "More Valid Data"])

    # Hidden validation sheet
    ws_hidden = wb.create_sheet(title="DV-IDENTITY-0")
    ws_hidden.sheet_state = "hidden"
    ws_hidden.append(["#REF!", "#VALUE!"])
    ws_hidden.append(["Secret Internal", "Data"])

    # VeryHidden sheet
    ws_very_hidden = wb.create_sheet(title="VeryHiddenSheet")
    ws_very_hidden.sheet_state = "veryHidden"
    ws_very_hidden.append(["HiddenCol", "HiddenVal"])

    wb.save(str(excel_path))

    result = excel_loader(excel_path)
    md = result["markdown"]

    # Visible content should be preserved
    assert "Valid Data" in md
    assert "More Valid Data" in md

    # Error codes should be filtered out
    assert "#REF!" not in md
    assert "#VALUE!" not in md

    # Hidden sheets must not appear
    assert "DV-IDENTITY-0" not in md
    assert "VeryHiddenSheet" not in md
    assert "Secret Internal" not in md
