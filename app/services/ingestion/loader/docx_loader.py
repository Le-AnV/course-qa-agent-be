"""Word document (.docx) loader module."""

import logging
from io import BytesIO
from typing import Any

import docx as docx_module
from PIL import Image

logger = logging.getLogger(__name__)


def docx_loader(file_path: str) -> dict[str, Any]:
    """Load a Word document (.docx), convert it to Markdown, and extract images.

    Args:
        file_path (str): The path to the Word document.

    Returns:
        dict[str, Any]: A dictionary containing the markdown output, metadata, and extracted images.
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
            table = docx_module.table.Table(element, doc)  # type: ignore
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
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to extract image %s from %s: %s", rel.target_ref, file_path, e)

    return {
        "markdown": "\n\n".join(markdown_output),
        "metadata": {"source": file_path, "type": "docx"},
        "images": images,
    }
