from pathlib import Path

import pytest

from app.services.ingestion.loader.text_loader import text_loader


def test_text_loader_markdown_success(tmp_path: Path):
    md_file = tmp_path / "test.md"
    md_file.write_text("# Chapter 1\n\nThis is a markdown lesson.", encoding="utf-8")

    result = text_loader(md_file)

    assert result["markdown"] == "# Chapter 1\n\nThis is a markdown lesson."
    assert result["metadata"]["type"] == "markdown"
    assert result["metadata"]["source"] == str(md_file)
    assert result["images"] == {}


def test_text_loader_plain_text_success(tmp_path: Path):
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("Linear Regression notes in plain text.\nLine 2.", encoding="utf-8")

    result = text_loader(txt_file)

    assert "Linear Regression notes" in result["markdown"]
    assert result["metadata"]["type"] == "text"
    assert result["metadata"]["source"] == str(txt_file)
    assert result["images"] == {}


def test_text_loader_empty_file_raises_value_error(tmp_path: Path):
    empty_file = tmp_path / "empty.md"
    empty_file.write_text("   \n\n  ", encoding="utf-8")

    with pytest.raises(ValueError, match="is empty or contains no readable text"):
        text_loader(empty_file)


def test_text_loader_missing_file_raises_file_not_found(tmp_path: Path):
    missing_file = tmp_path / "non_existent.md"

    with pytest.raises(FileNotFoundError):
        text_loader(missing_file)

