"""Document parser module for various file formats."""

import os
from io import BytesIO
from typing import Any, Dict

import docx as docx_module
import openpyxl
import pandas as pd
import pptx
import pypdfium2 as pdfium
from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from PIL import Image
from pptx.enum.shapes import MSO_SHAPE_TYPE

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


def extract_raw_embedded_images(file_path: str, min_size: int = 60) -> Dict[str, Image.Image]:
    """Extract raw embedded images from a PDF file.

    Args:
        file_path (str): The path to the PDF file.
        min_size (int, optional): Minimum width and height of the image to be extracted. Defaults to 60.

    Returns:
        Dict[str, Image.Image]: A dictionary mapping image filenames to PIL Image objects.
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


def pdf_loader(file_path: str) -> Dict[str, Any]:
    """Load and parse a PDF file into Markdown and extract images.

    Args:
        file_path (str): The path to the PDF file.

    Returns:
        Dict[str, Any]: A dictionary containing the generated markdown, metadata, and extracted images.
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


def docx_loader(file_path: str) -> Dict[str, Any]:
    """Load a Word document (.docx), convert it to Markdown, and extract images.

    Args:
        file_path (str): The path to the Word document.

    Returns:
        Dict[str, Any]: A dictionary containing the markdown output, metadata, and extracted images.
    """
    doc = docx_module.Document(file_path)
    markdown_output = []
    images = {}

    # Text and tables
    for element in doc.element.body:
        if element.tag.endswith("p"):
            para = docx_module.text.paragraph.Paragraph(element, doc)  # type: ignore
            if para.style.name.startswith("Heading"):
                level = para.style.name.replace("Heading ", "")
                try:
                    level_int = int(level)
                    markdown_output.append(f"{'#' * level_int} {para.text}")
                except ValueError:
                    markdown_output.append(f"# {para.text}")
            elif para.text.strip():
                markdown_output.append(para.text)
        elif element.tag.endswith("tbl"):
            table = docx_module.table.Table(element, doc) #type: ignore
            markdown_output.append("\n")
            for i, row in enumerate(table.rows):
                row_data = [cell.text.replace("\n", " ") for cell in row.cells]
                markdown_output.append("| " + " | ".join(row_data) + " |")
                if i == 0:
                    markdown_output.append("|" + "|".join(["---"] * len(row.cells)) + "|")
            markdown_output.append("\n")

    # Extract images
    img_idx = 0
    for rel in doc.part.rels.values():
        if "image" in rel.target_ref:
            try:
                img_bytes = rel.target_part.blob
                pil_img = Image.open(BytesIO(img_bytes))
                images[f"docx_img_{img_idx}.png"] = pil_img
                img_idx += 1
            except Exception:
                pass

    return {
        "markdown": "\n\n".join(markdown_output),
        "metadata": {"source": file_path, "type": "docx"},
        "images": images,
    }



def document_loader(file_path: str) -> Dict[str, Any]:
    """Automatically detect the file type and load it using the appropriate loader.

    Args:
        file_path (str): The path to the file.

    Raises:
        ValueError: If the file extension is not supported.

    Returns:
        Dict[str, Any]: A dictionary containing the markdown content, metadata, and extracted images.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        return pdf_loader(file_path)
    # elif ext in [".xlsx", ".xls"]:
    #     return excel_loader(file_path)
    elif ext == ".docx":
        return docx_loader(file_path)
    # elif ext == ".pptx":
    #     return pptx_loader(file_path)
    else:
        raise ValueError(f"Unsupported file format: {ext}")
