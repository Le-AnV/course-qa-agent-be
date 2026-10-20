"""Microsoft Word document (.docx and legacy .doc) loader module with heading preservation and image extraction."""

import logging
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

import docx as docx_module
import mammoth
from markitdown.converters._html_converter import HtmlConverter
from PIL import Image

logger = logging.getLogger(__name__)


def _convert_doc_to_docx(doc_path: Path, output_dir: Path) -> Path:
    """Convert legacy Microsoft Word .doc (Word 97-2003) to modern .docx format using headless office converter.

    Args:
        doc_path (Path): Path to the Microsoft Word .doc file.
        output_dir (Path): Temporary output directory for the converted .docx.

    Raises:
        RuntimeError: If the office converter (soffice/libreoffice) is not installed or conversion fails.
        FileNotFoundError: If the converted .docx file is not produced.

    Returns:
        Path: Path to the generated .docx file.
    """
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise RuntimeError(
            "Converting legacy Microsoft Word (.doc) files requires an office converter (soffice/libreoffice) installed on the system."
        )

    cmd = [
        binary,
        "--headless",
        "--convert-to",
        "docx",
        str(doc_path),
        "--outdir",
        str(output_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        error_msg = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"Failed to convert Microsoft Word .doc to .docx: {error_msg}")

    converted_file = output_dir / f"{doc_path.stem}.docx"
    if not converted_file.is_file():
        raise FileNotFoundError(f"Converted .docx file was not found at {converted_file}")

    return converted_file


def _build_style_map(docx_path: Path) -> str:
    """Determine dynamic style map based on whether document has a Title paragraph.

    If a document has a Title, Title becomes H1 (#) and Heading 1 becomes H2 (##).
    Otherwise, Heading 1 remains H1 (#).
    """
    try:
        doc = docx_module.Document(str(docx_path))
        has_title = any(
            p.style and p.style.name.lower() == "title" and p.text.strip()
            for p in doc.paragraphs
        )
    except Exception:  # noqa: BLE001
        has_title = False

    if has_title:
        return """
        p[style-name='Title'] => h1:fresh
        p[style-name='Subtitle'] => h2:fresh
        p[style-name='Heading 1'] => h2:fresh
        p[style-name='Heading 2'] => h3:fresh
        p[style-name='Heading 3'] => h4:fresh
        p[style-name='Heading 4'] => h5:fresh
        """
    return """
    p[style-name='Title'] => h1:fresh
    p[style-name='Subtitle'] => h2:fresh
    p[style-name='Heading 1'] => h1:fresh
    p[style-name='Heading 2'] => h2:fresh
    p[style-name='Heading 3'] => h3:fresh
    p[style-name='Heading 4'] => h4:fresh
    """


def docx_loader(file_path: str | Path) -> dict[str, Any]:
    """Load a Word document (.docx or .doc), convert to Markdown, and extract images.

    Args:
        file_path (str | Path): The path to the Word document.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is empty or contains no readable text.

    Returns:
        dict[str, Any]: A dictionary containing markdown content, metadata, and extracted images.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    ext = path.suffix.lower()
    doc_type = "doc" if ext == ".doc" else "docx"

    temp_dir_ctx = tempfile.TemporaryDirectory() if ext == ".doc" else None
    try:
        if temp_dir_ctx:
            target_docx = _convert_doc_to_docx(path, Path(temp_dir_ctx.name))
        else:
            target_docx = path

        style_map = _build_style_map(target_docx)

        img_idx = 0
        images: dict[str, Image.Image] = {}

        def image_handler(image: Any) -> dict[str, str]:
            nonlocal img_idx
            img_name = f"docx_img_{img_idx}.png"
            try:
                with image.open() as image_bytes:
                    pil_img = Image.open(BytesIO(image_bytes.read()))
                    images[img_name] = pil_img
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to decode image %s: %s", img_name, exc)
            img_idx += 1
            return {"src": img_name, "alt": img_name}

        with open(target_docx, "rb") as f:
            html_result = mammoth.convert_to_html(
                f,
                style_map=style_map,
                convert_image=mammoth.images.inline(image_handler),
            ).value

        html_converter = HtmlConverter()
        markdown_text = (html_converter.convert_string(html_result).text_content or "").strip()

        if not markdown_text and not images:
            raise ValueError(f"File '{path.name}' is empty or contains no readable text.")

        return {
            "markdown": markdown_text,
            "metadata": {"source": str(path), "type": doc_type},
            "images": images,
        }
    finally:
        if temp_dir_ctx:
            temp_dir_ctx.cleanup()
