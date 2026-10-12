"""Document parser and loader dispatcher module."""

import os
from pathlib import Path
from typing import Any

from app.services.ingestion.loader import (
    docx_loader,
    excel_loader,
    pdf_loader,
)

SUPPORTED_EXTENSIONS = {
    ".pdf": pdf_loader,
    ".docx": docx_loader,
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
