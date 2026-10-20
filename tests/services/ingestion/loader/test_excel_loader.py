from pathlib import Path

from openpyxl import Workbook

from app.services.ingestion.loader.excel_loader import excel_loader


def test_excel_loader_empty_workbook(tmp_path: Path):
    excel_path = tmp_path / "empty.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "EmptySheet"
    wb.save(str(excel_path))

    result = excel_loader(str(excel_path))

    assert "markdown" in result
    assert "metadata" in result
    assert "images" in result
    assert result["metadata"]["type"] == "excel"
    assert result["metadata"]["source"] == str(excel_path)
    assert result["markdown"] == ""


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
