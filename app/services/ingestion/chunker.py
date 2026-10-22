"""Document splitter and chunker module."""

import re

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.config.config import get_settings


def _clean_header_text(text: str) -> str:
    """Strip markdown formatting (*, _, ~~) and extra whitespace from headers."""
    return re.sub(r"(\*\*|__|\*|_|~~)", "", text).strip()


def markdown_chunker(
    markdown: str,
    metadata: dict | None = None,
    headers_to_split_on: list[tuple[str, str]] | None = None,
) -> list[Document]:
    """Split markdown text into chunks based on headers and add page numbers.

    Args:
        markdown: The markdown text to split.
        metadata: Optional dictionary containing metadata.
        headers_to_split_on: Optional list of tuples specifying headers to split on.

    Returns:
        List of Document chunks split by headers.
    """
    settings = get_settings()
    headers = headers_to_split_on or settings.headers_to_split_on

    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers, strip_headers=True)

    chunks = markdown_splitter.split_text(markdown)

    # 1. Clean markdown formatting in extracted header metadata
    for chunk in chunks:
        for k in list(chunk.metadata.keys()):
            if k.startswith("Header ") and isinstance(chunk.metadata[k], str):
                chunk.metadata[k] = _clean_header_text(chunk.metadata[k])

    # 2. Check for Slide locators <!-- Slide number: X -->
    slide_matches = list(
        re.finditer(r"<!--\s*Slide\s*number:\s*(\d+)\s*-->", markdown, flags=re.IGNORECASE)
    )

    # 3. Check for Page breaks <!-- page break -->
    page_breaks = [
        m.start() for m in re.finditer(r"<!--\s*page\s*break\s*-->", markdown, flags=re.IGNORECASE)
    ]

    is_excel = False
    if metadata:
        file_type = str(metadata.get("type", "") or metadata.get("file_type", "")).lower()
        file_name = str(metadata.get("file_name", "")).lower()
        if file_type in ("excel", "xlsx", "xls") or file_name.endswith((".xlsx", ".xls")):
            is_excel = True

    search_start = 0

    for chunk in chunks:
        # Find chunk position in original markdown
        snippet = chunk.page_content[:50].strip()
        if snippet:
            idx = markdown.find(snippet, search_start)
            if idx != -1:
                search_start = idx
            else:
                idx = search_start
        else:
            idx = search_start

        deepest_header = (
            chunk.metadata.get("Header 4")
            or chunk.metadata.get("Header 3")
            or chunk.metadata.get("Header 2")
            or chunk.metadata.get("Header 1")
        )

        if slide_matches:
            current_slide = 1
            for sm in slide_matches:
                if sm.start() <= idx:
                    current_slide = int(sm.group(1))
                else:
                    break
            chunk.metadata["slide_number"] = current_slide
            chunk.metadata["page_number"] = current_slide
            chunk.metadata["locator"] = f"Slide {current_slide}"
        elif page_breaks:
            page_num = 1 + sum(1 for pb in page_breaks if pb < idx)
            chunk.metadata["page_number"] = page_num
            chunk.metadata["locator"] = f"Page {page_num}"
        else:
            if deepest_header:
                chunk.metadata["locator"] = deepest_header
            if not is_excel:
                chunk.metadata["page_number"] = 1

    if metadata:
        for chunk in chunks:
            chunk.metadata.update(metadata)

    # For Excel: explicitly remove page_number as requested
    if is_excel:
        for chunk in chunks:
            chunk.metadata.pop("page_number", None)
            if "locator" not in chunk.metadata:
                deepest = (
                    chunk.metadata.get("Header 3")
                    or chunk.metadata.get("Header 2")
                    or chunk.metadata.get("Header 1")
                )
                if deepest:
                    chunk.metadata["locator"] = deepest

    # Clean up page break and slide number tags inside chunk contents
    for chunk in chunks:
        chunk.page_content = re.sub(
            r"<!--\s*page\s*break\s*-->\s*", "\n\n", chunk.page_content, flags=re.IGNORECASE
        )
        chunk.page_content = re.sub(
            r"<!--\s*Slide\s*number:\s*\d+\s*-->\s*", "\n\n", chunk.page_content, flags=re.IGNORECASE
        )
        chunk.page_content = chunk.page_content.strip()

    return chunks


def recursive_chunker(
    documents: list[Document],
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    separators: list[str] | None = None,
    metadata: dict | None = None,
) -> list[Document]:
    """Split documents into smaller chunks using recursive character splitting.

    Args:
        documents: List of Documents to split.
        chunk_size: Maximum character count per document chunk.
            Defaults to settings.chunk_size.
        chunk_overlap: Overlap character count between consecutive chunks.
            Defaults to settings.chunk_overlap.
        separators: List of string separators used for splitting.
            Defaults to settings.separators.
        metadata: Optional dictionary containing metadata (e.g., file name, class,
            chapter, starting page number) to add to or update in each chunk.

    Returns:
        List of recursively chunked Documents.
    """
    settings = get_settings()
    size = chunk_size if chunk_size is not None else settings.chunk_size
    overlap = chunk_overlap if chunk_overlap is not None else settings.chunk_overlap
    seps = separators if separators is not None else settings.separators

    splitter = RecursiveCharacterTextSplitter(
        separators=seps, chunk_size=size, chunk_overlap=overlap
    )

    chunks = splitter.split_documents(documents)

    if metadata:
        for chunk in chunks:
            chunk.metadata.update(metadata)

    return chunks
