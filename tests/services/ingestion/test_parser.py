from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.loader import powerpoint_loader
from app.services.ingestion.parser import SUPPORTED_EXTENSIONS, document_loader


def test_document_loader_unsupported_format(tmp_path: Path):
    file_path = tmp_path / "test.unsupported"
    file_path.write_text("sample content")

    with pytest.raises(ValueError, match="Unsupported file format: .unsupported"):
        document_loader(file_path)


def test_document_loader_routes_pdf(tmp_path: Path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_text("fake pdf")

    mock_pdf = MagicMock(return_value={"markdown": "pdf content", "metadata": {}, "images": {}})
    with patch.dict("app.services.ingestion.parser.SUPPORTED_EXTENSIONS", {".pdf": mock_pdf}):
        res = document_loader(str(pdf_path))

        mock_pdf.assert_called_once_with(str(pdf_path))
        assert res["markdown"] == "pdf content"


def test_document_loader_routes_docx_and_doc(tmp_path: Path):
    docx_path = tmp_path / "sample.docx"
    docx_path.write_text("fake docx")
    doc_path = tmp_path / "sample.doc"
    doc_path.write_text("fake doc")

    mock_docx = MagicMock(return_value={"markdown": "doc content", "metadata": {}, "images": {}})
    with patch.dict(
        "app.services.ingestion.parser.SUPPORTED_EXTENSIONS",
        {".docx": mock_docx, ".doc": mock_docx},
    ):
        res_docx = document_loader(docx_path)
        mock_docx.assert_called_with(str(docx_path))
        assert res_docx["markdown"] == "doc content"

        res_doc = document_loader(str(doc_path))
        mock_docx.assert_called_with(str(doc_path))
        assert res_doc["markdown"] == "doc content"


def test_document_loader_routes_xlsx_and_xls(tmp_path: Path):
    xlsx_path = tmp_path / "sample.xlsx"
    xlsx_path.write_text("fake xlsx")
    xls_path = tmp_path / "sample.xls"
    xls_path.write_text("fake xls")

    mock_excel = MagicMock(return_value={"markdown": "excel content", "metadata": {}, "images": {}})
    with patch.dict(
        "app.services.ingestion.parser.SUPPORTED_EXTENSIONS",
        {".xlsx": mock_excel, ".xls": mock_excel},
    ):
        res_xlsx = document_loader(xlsx_path)
        mock_excel.assert_called_with(str(xlsx_path))
        assert res_xlsx["markdown"] == "excel content"

        res_xls = document_loader(str(xls_path))
        mock_excel.assert_called_with(str(xls_path))
        assert res_xls["markdown"] == "excel content"


def test_document_loader_routes_md_and_txt(tmp_path: Path):
    md_path = tmp_path / "sample.md"
    md_path.write_text("# Markdown content", encoding="utf-8")
    txt_path = tmp_path / "sample.txt"
    txt_path.write_text("Plain text content", encoding="utf-8")

    mock_text = MagicMock(return_value={"markdown": "text content", "metadata": {}, "images": {}})
    with patch.dict(
        "app.services.ingestion.parser.SUPPORTED_EXTENSIONS",
        {".md": mock_text, ".txt": mock_text},
    ):
        res_md = document_loader(md_path)
        mock_text.assert_called_with(str(md_path))
        assert res_md["markdown"] == "text content"

        res_txt = document_loader(str(txt_path))
        mock_text.assert_called_with(str(txt_path))
        assert res_txt["markdown"] == "text content"


def test_document_loader_routes_pptx_and_ppt(tmp_path: Path):
    assert SUPPORTED_EXTENSIONS[".pptx"] == powerpoint_loader
    assert SUPPORTED_EXTENSIONS[".ppt"] == powerpoint_loader

    pptx_path = tmp_path / "sample.pptx"
    pptx_path.write_text("fake pptx")
    ppt_path = tmp_path / "sample.ppt"
    ppt_path.write_text("fake ppt")

    mock_pptx = MagicMock(return_value={"markdown": "presentation content", "metadata": {}, "images": {}})
    with patch.dict(
        "app.services.ingestion.parser.SUPPORTED_EXTENSIONS",
        {".pptx": mock_pptx, ".ppt": mock_pptx},
    ):
        res_pptx = document_loader(pptx_path)
        mock_pptx.assert_called_with(str(pptx_path))
        assert res_pptx["markdown"] == "presentation content"

        res_ppt = document_loader(str(ppt_path))
        mock_pptx.assert_called_with(str(ppt_path))
        assert res_ppt["markdown"] == "presentation content"


def test_document_loader_cleans_markdown(tmp_path: Path):
    sample_path = tmp_path / "sample.md"
    sample_path.write_text("# dummy", encoding="utf-8")

    raw_md = "##Header\nLine with space   \n\n\n\nPage 1\nContent"
    mock_loader = MagicMock(
        side_effect=lambda path: {"markdown": raw_md, "metadata": {}, "images": {}}
    )


    with patch.dict(
        "app.services.ingestion.parser.SUPPORTED_EXTENSIONS",
        {".md": mock_loader},
    ):
        res_clean = document_loader(sample_path)
        assert res_clean["markdown"] == "## Header\nLine with space\n\nContent"
        assert "Page 1" not in res_clean["markdown"]

        res_raw = document_loader(sample_path, clean=False)
        assert res_raw["markdown"] == raw_md



