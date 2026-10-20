"""Document parser and loader dispatcher module."""

import os
from pathlib import Path
from typing import Any

from app.services.ingestion.cleaner import clean_markdown
from app.services.ingestion.loader import (
    docx_loader,
    excel_loader,
    pdf_loader,
    powerpoint_loader,
    text_loader,
)

SUPPORTED_EXTENSIONS = {
    ".pdf": pdf_loader,
    ".docx": docx_loader,
    ".doc": docx_loader,
    ".xlsx": excel_loader,
    ".xls": excel_loader,
    ".pptx": powerpoint_loader,
    ".ppt": powerpoint_loader,
    ".md": text_loader,
    ".txt": text_loader,
}


def document_loader(
    file_path: str | Path,
    clean: bool = True,
    watermarks: list[str] | None = None,
) -> dict[str, Any]:
    """Automatically detect the file type, load it using the appropriate loader,
    and clean/standardize the extracted Markdown.

    Args:
        file_path (str | Path): The path to the file.
        clean (bool, optional): Whether to clean the extracted Markdown. Defaults to True.
        watermarks (list[str] | None, optional): Optional list of watermark strings to strip.

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

    result = loader(path_str)

    if clean and "markdown" in result and result["markdown"]:
        cleaned_text = clean_markdown(result["markdown"], watermarks=watermarks)
        result = {**result, "markdown": cleaned_text}

    return result



__all__ = [
    "clean_markdown",
    "document_loader",
    "docx_loader",
    "excel_loader",
    "pdf_loader",
    "powerpoint_loader",
    "text_loader",
]
