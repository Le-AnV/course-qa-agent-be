"""Document parser and loader dispatcher module."""

import os
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile


def pdf_loader(file_path: str) -> dict[str, Any]:
    from app.services.ingestion.loader import pdf_loader as load_pdf

    return load_pdf(file_path)


def docx_loader(file_path: str) -> dict[str, Any]:
    from app.services.ingestion.loader import docx_loader as load_docx

    return load_docx(file_path)


def excel_loader(file_path: str) -> dict[str, Any]:
    from app.services.ingestion.loader import excel_loader as load_excel

    return load_excel(file_path)


def _text_loader(file_path: str) -> dict[str, Any]:
    path = Path(file_path)
    return {
        "markdown": path.read_text(encoding="utf-8"),
        "metadata": {"source": file_path, "type": path.suffix.lower().lstrip(".")},
        "images": {},
    }


def _pptx_loader(file_path: str) -> dict[str, Any]:
    slide_pattern = re.compile(r"ppt/slides/slide(\d+)\.xml$")
    sections: list[str] = []
    with ZipFile(file_path) as archive:
        slide_paths = sorted(
            (name for name in archive.namelist() if slide_pattern.fullmatch(name)),
            key=lambda name: int(slide_pattern.fullmatch(name).group(1)),  # type: ignore[union-attr]
        )
        for slide_index, slide_path in enumerate(slide_paths, start=1):
            root = ElementTree.fromstring(archive.read(slide_path))
            paragraphs = []
            for paragraph in root.iter():
                if not paragraph.tag.endswith("}p"):
                    continue
                text = "".join(
                    node.text or ""
                    for node in paragraph.iter()
                    if node.tag.endswith("}t")
                ).strip()
                if text:
                    paragraphs.append(text)
            if paragraphs:
                sections.append(f"## Slide {slide_index}\n\n" + "\n\n".join(paragraphs))
            if slide_index < len(slide_paths):
                sections.append("<!-- page break -->")

    return {
        "markdown": "\n\n".join(sections),
        "metadata": {"source": file_path, "type": "pptx"},
        "images": {},
    }


SUPPORTED_EXTENSIONS = {
    ".pdf": pdf_loader,
    ".docx": docx_loader,
    ".md": _text_loader,
    ".txt": _text_loader,
    ".pptx": _pptx_loader,
    ".xlsx": excel_loader,
    ".xls": excel_loader,
}


def document_loader(file_path: str | Path) -> dict[str, Any]:
    """Automatically detect the file type and load it using the appropriate loader.

    Args:
        file_path (str | Path): The path to the file.

    Raises:
        ValueError: If the file extension is not supported.

    Returns:
        dict[str, Any]: A dictionary containing markdown content, metadata, and extracted images.
    """
    path_str = str(file_path)
    ext = os.path.splitext(path_str)[1].lower()

    loader = SUPPORTED_EXTENSIONS.get(ext)
    if loader is None:
        raise ValueError(f"Unsupported file format: {ext}")

    return loader(path_str)


__all__ = [
    "document_loader",
    "docx_loader",
    "excel_loader",
    "pdf_loader",
]
