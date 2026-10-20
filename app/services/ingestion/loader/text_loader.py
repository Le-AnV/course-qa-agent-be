"""Markdown (.md) and Plain Text (.txt) document loader module."""

from pathlib import Path
from typing import Any


def text_loader(file_path: str | Path) -> dict[str, Any]:
    """Load a Markdown or Plain Text file and normalize it for ingestion.

    Args:
        file_path (str | Path): The path to the text or markdown file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is empty or contains only whitespace.

    Returns:
        dict[str, Any]: A dictionary containing markdown content, metadata, and empty images dict.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="utf-8-sig")

    if not content.strip():
        raise ValueError(f"File '{path.name}' is empty or contains no readable text.")

    ext = path.suffix.lower()
    doc_type = "markdown" if ext == ".md" else "text"

    return {
        "markdown": content,
        "metadata": {"source": str(path), "type": doc_type},
        "images": {},
    }

