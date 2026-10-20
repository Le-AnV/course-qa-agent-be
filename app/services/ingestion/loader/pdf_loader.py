"""PDF document loader module using PyMuPDF4LLM."""

from pathlib import Path
from typing import Any

import pymupdf4llm
import pypdfium2 as pdfium
from PIL import Image


def extract_raw_embedded_images(file_path: str, min_size: int = 60) -> dict[str, Image.Image]:
    """Extract raw embedded images from a PDF file.

    Args:
        file_path (str): The path to the PDF file.
        min_size (int, optional): Minimum width and height of the image to be extracted. Defaults to 60.

    Returns:
        dict[str, Image.Image]: A dictionary mapping image filenames to PIL Image objects.
    """
    doc = pdfium.PdfDocument(str(file_path))
    images = {}
    for p_idx, page in enumerate(doc):
        img_idx = 0
        for obj in page.get_objects():
            if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE:  # type: ignore
                bitmap = obj.get_bitmap()  # type: ignore
                if bitmap.width >= min_size and bitmap.height >= min_size:
                    images[f"_page_{p_idx}_embedded_{img_idx}.png"] = bitmap.to_pil()
                    img_idx += 1
    return images


def pdf_loader(file_path: str | Path, extract_images: bool = True) -> dict[str, Any]:
    """Load and parse a PDF file into Markdown with preserved headings using PyMuPDF4LLM.

    Args:
        file_path (str | Path): The path to the PDF file.
        extract_images (bool): Whether to extract embedded images. Defaults to True.

    Returns:
        dict[str, Any]: A dictionary containing markdown, metadata, and extracted images.

    Raises:
        FileNotFoundError: If the PDF file does not exist.
        ValueError: If the PDF file is empty or contains no readable text or images.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF file not found: {file_path}")

    # Convert PDF to structured Markdown (with font-size-based headings #, ##, etc.)
    markdown_content = pymupdf4llm.to_markdown(str(path))

    images: dict[str, Image.Image] = {}
    if extract_images:
        images = extract_raw_embedded_images(str(path))

    if not markdown_content.strip() and not images:
        raise ValueError(f"PDF file '{path.name}' is empty or contains no readable text/images.")

    metadata = {
        "source": str(path),
        "file_name": path.name,
        "extension": path.suffix.lower(),
        "type": "pdf",
        "loader": "pymupdf4llm",
    }


    return {
        "markdown": markdown_content,
        "metadata": metadata,
        "images": images,
    }
