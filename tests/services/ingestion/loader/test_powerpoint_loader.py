from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from app.services.ingestion.loader.powerpoint_loader import powerpoint_loader, pptx_loader


def test_powerpoint_loader_extracts_slides_and_notes(tmp_path: Path):
    pptx_path = tmp_path / "test_presentation.pptx"

    prs = Presentation()

    # Slide 1: Title, subtitle/bullet, speaker notes
    slide_1 = prs.slides.add_slide(prs.slide_layouts[0])
    slide_1.shapes.title.text = "Introduction to Python"
    slide_1.placeholders[1].text = "Welcome to the course\nFirst semester"

    notes_slide = slide_1.notes_slide
    notes_slide.notes_text_frame.text = "Remember to record the lecture."

    # Slide 2: Table
    slide_2 = prs.slides.add_slide(prs.slide_layouts[5])  # Title only
    slide_2.shapes.title.text = "Syllabus Overview"

    # Add a 2x2 table
    rows, cols = 2, 2
    left, top, width, height = Inches(1), Inches(2), Inches(4), Inches(2)
    table_shape = slide_2.shapes.add_table(rows, cols, left, top, width, height)
    table = table_shape.table
    table.cell(0, 0).text = "Week"
    table.cell(0, 1).text = "Topic"
    table.cell(1, 0).text = "1"
    table.cell(1, 1).text = "Syntax"

    prs.save(str(pptx_path))

    result = powerpoint_loader(pptx_path)

    assert "<!-- Slide number: 1 -->" in result["markdown"]
    assert "# Introduction to Python" in result["markdown"]
    assert "Welcome to the course" in result["markdown"]
    assert "### Notes:\nRemember to record the lecture." in result["markdown"]

    assert "<!-- Slide number: 2 -->" in result["markdown"]
    assert "# Syllabus Overview" in result["markdown"]
    assert "| Week | Topic |" in result["markdown"]
    assert "| 1 | Syntax |" in result["markdown"]

    assert result["metadata"]["type"] == "pptx"
    assert result["metadata"]["total_slides"] == 2
    assert result["metadata"]["source"] == str(pptx_path)


def test_powerpoint_loader_alias_compatibility(tmp_path: Path):
    pptx_path = tmp_path / "test_alias.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Alias Test"
    prs.save(str(pptx_path))

    result = pptx_loader(pptx_path)
    assert "# Alias Test" in result["markdown"]


def test_powerpoint_loader_extracts_images(tmp_path: Path):
    pptx_path = tmp_path / "test_images.pptx"

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Slide With Image"

    # Create dummy image in memory
    img = Image.new("RGB", (100, 100), color="blue")
    img_io = BytesIO()
    img.save(img_io, format="PNG")
    img_io.seek(0)

    # Insert image into slide
    slide.shapes.add_picture(img_io, Inches(1), Inches(1), width=Inches(2))

    prs.save(str(pptx_path))

    result = powerpoint_loader(pptx_path)

    assert "slide_1_img_0.png" in result["images"]
    assert "![slide_1_img_0.png](slide_1_img_0.png)" in result["markdown"]
    assert isinstance(result["images"]["slide_1_img_0.png"], Image.Image)


def test_powerpoint_loader_empty_raises_value_error(tmp_path: Path):
    pptx_path = tmp_path / "empty.pptx"

    prs = Presentation()
    # Add a blank slide with no text and no shapes
    prs.slides.add_slide(prs.slide_layouts[6])  # Blank layout
    prs.save(str(pptx_path))

    with pytest.raises(ValueError, match="is empty or contains no readable text"):
        powerpoint_loader(pptx_path)


def test_powerpoint_loader_missing_file_raises_file_not_found(tmp_path: Path):
    missing_path = tmp_path / "non_existent.pptx"
    with pytest.raises(FileNotFoundError):
        powerpoint_loader(missing_path)


def test_powerpoint_loader_ppt_conversion(tmp_path: Path):
    fake_ppt = tmp_path / "legacy.ppt"
    fake_ppt.write_bytes(b"dummy ppt bytes")

    dummy_pptx = tmp_path / "converted.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Legacy Converted Presentation"
    prs.save(str(dummy_pptx))

    with patch(
        "app.services.ingestion.loader.powerpoint_loader._convert_ppt_to_pptx",
        return_value=dummy_pptx,
    ) as mock_convert:
        result = powerpoint_loader(fake_ppt)

        mock_convert.assert_called_once()
        assert "# Legacy Converted Presentation" in result["markdown"]
        assert result["metadata"]["type"] == "ppt"
