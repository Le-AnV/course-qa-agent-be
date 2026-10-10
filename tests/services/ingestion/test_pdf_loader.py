from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.ingestion.loader.pdf_loader import pdf_loader


def test_pdf_loader_with_converter(tmp_path: Path):
    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_text("dummy")

    mock_rendered = MagicMock()
    mock_rendered.markdown = "# Chapter 1\nPDF content"
    mock_rendered.metadata = {"pages": 1}
    mock_rendered.images = {"img1.png": MagicMock()}

    with patch("app.services.ingestion.loader.pdf_loader._CONVERTER", return_value=mock_rendered):
        result = pdf_loader(str(fake_pdf))

        assert result["markdown"] == "# Chapter 1\nPDF content"
        assert result["metadata"] == {"pages": 1}
        assert "img1.png" in result["images"]


def test_pdf_loader_fallback_images_when_none(tmp_path: Path):
    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_text("dummy")

    mock_rendered = MagicMock()
    mock_rendered.markdown = "No images found by marker"
    mock_rendered.metadata = {}
    mock_rendered.images = {}

    with (
        patch("app.services.ingestion.loader.pdf_loader._CONVERTER", return_value=mock_rendered),
        patch(
            "app.services.ingestion.loader.pdf_loader.extract_raw_embedded_images",
            return_value={"raw_0.png": MagicMock()},
        ) as mock_extract,
    ):
        result = pdf_loader(str(fake_pdf))

        mock_extract.assert_called_once_with(str(fake_pdf))
        assert "raw_0.png" in result["images"]

