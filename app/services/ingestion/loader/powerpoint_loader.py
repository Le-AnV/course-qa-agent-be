"""Microsoft PowerPoint presentation (.pptx and legacy .ppt) loader module with slide locator and image extraction."""

import logging
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

logger = logging.getLogger(__name__)


def _convert_ppt_to_pptx(ppt_path: Path, output_dir: Path) -> Path:
    """Convert legacy Microsoft PowerPoint .ppt (PowerPoint 97-2003) to modern .pptx format using headless office converter.

    Args:
        ppt_path (Path): Path to the Microsoft PowerPoint .ppt file.
        output_dir (Path): Temporary output directory for the converted .pptx.

    Raises:
        RuntimeError: If the office converter (soffice/libreoffice) is not installed or conversion fails.
        FileNotFoundError: If the converted .pptx file is not produced.

    Returns:
        Path: Path to the generated .pptx file.
    """
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise RuntimeError(
            "Converting legacy Microsoft PowerPoint (.ppt) files requires an office converter (soffice/libreoffice) installed on the system."
        )

    cmd = [
        binary,
        "--headless",
        "--convert-to",
        "pptx",
        str(ppt_path),
        "--outdir",
        str(output_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        error_msg = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"Failed to convert Microsoft PowerPoint .ppt to .pptx: {error_msg}")

    converted_file = output_dir / f"{ppt_path.stem}.pptx"
    if not converted_file.is_file():
        raise FileNotFoundError(f"Converted .pptx file was not found at {converted_file}")

    return converted_file


def _convert_table_to_markdown(table: Any) -> str:
    """Convert a PowerPoint table shape to a Markdown table string."""
    rows_data: list[list[str]] = []
    for row in table.rows:
        row_text = [cell.text.strip().replace("\n", " ").replace("|", "\\|") for cell in row.cells]
        rows_data.append(row_text)

    if not rows_data:
        return ""

    col_count = len(rows_data[0])
    if col_count == 0:
        return ""

    headers = rows_data[0]
    separator = ["---"] * col_count

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for r in rows_data[1:]:
        r_padded = r + [""] * (col_count - len(r))
        lines.append("| " + " | ".join(r_padded[:col_count]) + " |")

    return "\n".join(lines)


def _process_shape(
    shape: Any,
    slide_idx: int,
    title_shape: Any,
    slide_parts: list[str],
    images: dict[str, Image.Image],
    img_counter: list[int],
) -> None:
    """Recursively process a presentation shape and extract text, tables, or images."""
    # Skip title shape since it's already rendered in slide header
    if title_shape and shape == title_shape:
        return

    # Pictures
    if (
        shape.shape_type == MSO_SHAPE_TYPE.PICTURE
        or (shape.shape_type == MSO_SHAPE_TYPE.PLACEHOLDER and hasattr(shape, "image"))
    ):
        try:
            img_bytes = shape.image.blob
            pil_img = Image.open(BytesIO(img_bytes))
            img_name = f"slide_{slide_idx}_img_{img_counter[0]}.png"
            images[img_name] = pil_img
            slide_parts.append(f"![{img_name}]({img_name})")
            img_counter[0] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to extract image on slide %d: %s", slide_idx, exc)
        return

    # Tables
    if shape.has_table:
        table_md = _convert_table_to_markdown(shape.table)
        if table_md.strip():
            slide_parts.append(table_md)
        return

    # Group Shapes
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for subshape in sorted(
            shape.shapes,
            key=lambda s: (s.top if s.top is not None else 0, s.left if s.left is not None else 0),
        ):
            _process_shape(subshape, slide_idx, title_shape, slide_parts, images, img_counter)
        return

    # Text frames
    if shape.has_text_frame:
        paragraphs_text: list[str] = []
        for p in shape.text_frame.paragraphs:
            txt = p.text.strip()
            if not txt:
                continue
            if p.level > 0:
                indent = "  " * p.level
                paragraphs_text.append(f"{indent}- {txt}")
            elif len(shape.text_frame.paragraphs) > 1 and not txt.startswith(("- ", "* ")):
                paragraphs_text.append(f"- {txt}")
            else:
                paragraphs_text.append(txt)

        if paragraphs_text:
            slide_parts.append("\n".join(paragraphs_text))


def powerpoint_loader(file_path: str | Path) -> dict[str, Any]:
    """Load and parse a PowerPoint presentation (.pptx or .ppt) into Markdown and extract images.

    Args:
        file_path (str | Path): The path to the PowerPoint file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the presentation is empty or contains no readable text or images.

    Returns:
        dict[str, Any]: A dictionary containing markdown content, metadata, and extracted images.
    """

    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    ext = path.suffix.lower()
    doc_type = "ppt" if ext == ".ppt" else "pptx"

    temp_dir_ctx = tempfile.TemporaryDirectory() if ext == ".ppt" else None
    try:
        if temp_dir_ctx:
            target_pptx = _convert_ppt_to_pptx(path, Path(temp_dir_ctx.name))
        else:
            target_pptx = path

        prs = Presentation(str(target_pptx))
        slides_md: list[str] = []
        images: dict[str, Image.Image] = {}

        for slide_idx, slide in enumerate(prs.slides, start=1):
            slide_parts: list[str] = []
            img_counter = [0]

            # Title detection
            title_text = ""
            title_shape = slide.shapes.title
            if title_shape and title_shape.has_text_frame and title_shape.text.strip():
                title_text = title_shape.text.strip()

            header = f"<!-- Slide number: {slide_idx} -->\n"
            if title_text:
                header += f"# {title_text}"
            else:
                header += f"# Slide {slide_idx}"
            slide_parts.append(header)

            # Sort shapes in reading order (top to bottom, left to right)
            sorted_shapes = sorted(
                slide.shapes,
                key=lambda s: (s.top if s.top is not None else 0, s.left if s.left is not None else 0),
            )
            for shp in sorted_shapes:
                _process_shape(shp, slide_idx, title_shape, slide_parts, images, img_counter)

            # Speaker Notes
            if slide.has_notes_slide:
                notes_frame = slide.notes_slide.notes_text_frame
                if notes_frame and notes_frame.text.strip():
                    slide_parts.append(f"### Notes:\n{notes_frame.text.strip()}")

            slide_content = "\n\n".join(p for p in slide_parts if p.strip())
            slides_md.append(slide_content)

        markdown_text = "\n\n<!-- page break -->\n\n".join(s for s in slides_md if s.strip()).strip()

        # Check for empty presentation
        # Notice: a presentation is only non-empty if it contains actual content beyond just default slide headers
        content_lines = [
            line
            for line in markdown_text.splitlines()
            if line.strip()
            and not line.startswith("<!-- Slide number:")
            and not line.startswith("<!-- page break")
            and line not in [f"# Slide {i}" for i in range(1, len(prs.slides) + 2)]
        ]
        if not content_lines and not images:
            raise ValueError(f"File '{path.name}' is empty or contains no readable text.")

        return {
            "markdown": markdown_text,
            "metadata": {
                "source": str(path),
                "type": doc_type,
                "total_slides": len(prs.slides),
            },
            "images": images,
        }
    finally:
        if temp_dir_ctx:
            temp_dir_ctx.cleanup()


# Backward compatibility alias
pptx_loader = powerpoint_loader


