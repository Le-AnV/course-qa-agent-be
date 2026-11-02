from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.loader.pdf_loader import pdf_loader


def test_pdf_loader_success(tmp_path: Path):
    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 dummy")

    with (
        patch("pymupdf4llm.to_markdown", return_value="# Title 1\n## Section 1.1\nContent"),
        patch(
            "app.services.ingestion.loader.pdf_loader.extract_raw_embedded_images",
            return_value={"img_0.png": MagicMock()},
        ),
    ):
        result = pdf_loader(fake_pdf)

        assert "# Title 1" in result["markdown"]
        assert "## Section 1.1" in result["markdown"]
        assert result["metadata"]["loader"] == "pymupdf4llm"
        assert result["metadata"]["file_name"] == "test.pdf"
        assert result["metadata"]["type"] == "pdf"
        assert "img_0.png" in result["images"]


def test_pdf_loader_with_page_chunks(tmp_path: Path):
    fake_pdf = tmp_path / "multipage.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 dummy")

    chunks_data = [
        {"metadata": {"page": 2}, "text": ""},
        {"metadata": {"page": 5}, "text": "Page 5 content"},
        {"metadata": {"page": 9}, "text": "Page 9 content"},
    ]

    with (
        patch("pymupdf4llm.to_markdown", return_value=chunks_data),
        patch(
            "app.services.ingestion.loader.pdf_loader.extract_raw_embedded_images",
            return_value={},
        ),
    ):
        result = pdf_loader(fake_pdf, extract_images=False)
        assert "<!-- Page number: 2 -->" in result["markdown"]
        assert "<!-- Page number: 5 -->" in result["markdown"]
        assert "<!-- Page number: 9 -->" in result["markdown"]
        assert "Page 5 content" in result["markdown"]
        assert "Page 9 content" in result["markdown"]


def test_pdf_loader_without_extract_images(tmp_path: Path):
    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 dummy")

    with (
        patch("pymupdf4llm.to_markdown", return_value="# Title 1\nContent"),
        patch(
            "app.services.ingestion.loader.pdf_loader.extract_raw_embedded_images",
            return_value={"img_0.png": MagicMock()},
        ) as mock_extract,
    ):
        result = pdf_loader(fake_pdf, extract_images=False)

        assert result["images"] == {}
        mock_extract.assert_not_called()


def test_pdf_loader_empty_raises_value_error(tmp_path: Path):
    fake_pdf = tmp_path / "empty.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 empty")

    with (
        patch("pymupdf4llm.to_markdown", return_value="   \n\n  "),
        patch(
            "app.services.ingestion.loader.pdf_loader.extract_raw_embedded_images",
            return_value={},
        ),
        pytest.raises(ValueError, match="is empty or contains no readable text"),
    ):
        pdf_loader(fake_pdf)


def test_pdf_loader_missing_file_raises_file_not_found(tmp_path: Path):
    missing_pdf = tmp_path / "missing.pdf"
    with pytest.raises(FileNotFoundError):
        pdf_loader(missing_pdf)
