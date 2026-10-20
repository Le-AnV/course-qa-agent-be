from pathlib import Path
from unittest.mock import patch

import docx
import pytest

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
    assert "Week" in md
    assert "Topic" in md
    assert "Overview" in md


def test_docx_loader_empty_file_raises_value_error(tmp_path: Path):
    doc_path = tmp_path / "empty.docx"
    doc = docx.Document()
    doc.save(str(doc_path))

    with pytest.raises(ValueError, match="is empty or contains no readable text"):
        docx_loader(doc_path)


def test_docx_loader_doc_extension(tmp_path: Path):
    doc_path = tmp_path / "sample.doc"
    doc_path.write_bytes(b"dummy doc binary")

    mock_docx_path = tmp_path / "converted.docx"
    doc = docx.Document()
    doc.add_heading("Converted Document", level=1)
    doc.add_paragraph("Content from doc.")
    doc.save(str(mock_docx_path))

    with patch(
        "app.services.ingestion.loader.docx_loader._convert_doc_to_docx",
        return_value=mock_docx_path,
    ):
        result = docx_loader(doc_path)

        assert result["metadata"]["type"] == "doc"
        assert "# Converted Document" in result["markdown"]
        assert "Content from doc." in result["markdown"]


def test_docx_loader_real_sample_doc_if_exists():
    sample_doc = Path(".vscode/data/books/file-sample_1MB.doc")
    if sample_doc.is_file():
        result = docx_loader(sample_doc)
        assert result["metadata"]["type"] == "doc"
        assert len(result["markdown"]) > 0
