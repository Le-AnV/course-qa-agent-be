from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.parser import document_loader


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


def test_document_loader_routes_docx(tmp_path: Path):
    docx_path = tmp_path / "sample.docx"
    docx_path.write_text("fake docx")

    mock_docx = MagicMock(return_value={"markdown": "docx content", "metadata": {}, "images": {}})
    with patch.dict("app.services.ingestion.parser.SUPPORTED_EXTENSIONS", {".docx": mock_docx}):
        res = document_loader(docx_path)

        mock_docx.assert_called_once_with(str(docx_path))
        assert res["markdown"] == "docx content"


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
