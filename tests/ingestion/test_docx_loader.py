from pathlib import Path

import docx

from app.services.ingestion.loader.docx_loader import docx_loader


def test_docx_loader_headings_and_tables(tmp_path: Path):
    doc_path = tmp_path / "sample.docx"
    doc = docx.Document()

    # Headings and paragraphs
    doc.add_heading("Course Introduction", level=1)
    doc.add_heading("Prerequisites", level=2)
    doc.add_paragraph("This course requires basic programming skills.")

    # Table
    table = doc.add_table(rows=2, cols=2)
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = "Week"
    hdr_cells[1].text = "Topic"
    row_cells = table.rows[1].cells
    row_cells[0].text = "1"
    row_cells[1].text = "Overview"

    doc.save(str(doc_path))

    result = docx_loader(str(doc_path))

    assert "markdown" in result
    assert "metadata" in result
    assert "images" in result

    assert result["metadata"]["type"] == "docx"
    assert result["metadata"]["source"] == str(doc_path)

    md = result["markdown"]
    assert "# Course Introduction" in md
    assert "## Prerequisites" in md
    assert "This course requires basic programming skills." in md
    assert "| Week | Topic |" in md
    assert "| 1 | Overview |" in md
