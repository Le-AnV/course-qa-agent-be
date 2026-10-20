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
