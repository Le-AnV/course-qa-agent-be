"""PDF document loader module."""

from typing import Any

import pypdfium2 as pdfium
from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from PIL import Image

# Configure marker PDF converter
_marker_config = {
    "output_format": "markdown",
    "mode": "fast",
    "disable_ocr": True,
}
_config_parser = ConfigParser(_marker_config)
_MODEL_DICT = create_model_dict()
_CONVERTER = PdfConverter(
    config=_config_parser.generate_config_dict(),
    artifact_dict=_MODEL_DICT,
)


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


def pdf_loader(file_path: str) -> dict[str, Any]:
    """Load and parse a PDF file into Markdown and extract images.

    Args:
        file_path (str): The path to the PDF file.

    Returns:
        dict[str, Any]: A dictionary containing the generated markdown, metadata, and extracted images.
    """
    rendered = _CONVERTER(file_path)

    images = dict(rendered.images) if rendered.images else {}
    if not images:
        images = extract_raw_embedded_images(file_path)

    return {
        "markdown": rendered.markdown,
        "metadata": rendered.metadata,
        "images": images,
    }
