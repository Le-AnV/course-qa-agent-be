"""Document splitter and chunker module."""

import re

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.config.config import get_settings


def _clean_header_text(text: str) -> str:
    """Strip markdown formatting (*, _, ~~) and extra whitespace from headers."""
    return re.sub(r"(\*\*|__|\*|_|~~)", "", text).strip()


def _get_deepest_header(metadata: dict) -> str | None:
    """Return the deepest available markdown header from metadata."""
    return (
        metadata.get("Header 4")
        or metadata.get("Header 3")
        or metadata.get("Header 2")
        or metadata.get("Header 1")
    )


def markdown_chunker(
    markdown: str,
    metadata: dict | None = None,
    headers_to_split_on: list[tuple[str, str]] | None = None,
) -> list[Document]:
    """Split markdown text into semantic chunks based on headers, tracking locators and page numbers.

    All loaders in `app/services/ingestion/loader` output structured Markdown with standard markers:
    - PowerPoint: contains `<!-- Slide number: X -->` tags.
    - PDF & Word: contain `<!-- page break -->` tags.
    - Excel, Web & Plain Text: unpaginated content organized by section headers (`#`, `##`, `###`).

    Args:
        markdown: The markdown text to split.
        metadata: Optional dictionary containing document metadata (e.g. file_name, class_id).
        headers_to_split_on: Optional list of tuples specifying headers to split on.

    Returns:
        List of Document chunks with normalized metadata and sanitized text.
    """
    settings = get_settings()
    headers = headers_to_split_on or settings.headers_to_split_on

    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers, strip_headers=True)
    chunks = markdown_splitter.split_text(markdown)

    meta = metadata or {}

    # Pre-index slide markers and page break positions in raw markdown
    slide_matches = list(
        re.finditer(r"<!--\s*Slide\s*number:\s*(\d+)\s*-->", markdown, flags=re.IGNORECASE)
    )
    page_breaks = [
        m.start() for m in re.finditer(r"<!--\s*page\s*break\s*-->", markdown, flags=re.IGNORECASE)
    ]

    search_start = 0

    for chunk in chunks:
        # 1. Clean markdown styling inside extracted header metadata
        for k in list(chunk.metadata.keys()):
            if k.startswith("Header ") and isinstance(chunk.metadata[k], str):
                chunk.metadata[k] = _clean_header_text(chunk.metadata[k])

        # 2. Inherit base document metadata (base metadata first, chunk metadata overrides)
        chunk.metadata = {**meta, **chunk.metadata}

        # 3. Locate chunk start position in original markdown text
        snippet = chunk.page_content[:50].strip()
        idx = markdown.find(snippet, search_start) if snippet else search_start
        if idx != -1:
            search_start = idx
        else:
            idx = search_start

        # 4. Resolve locator & pagination based on structural markers
        deepest_header = _get_deepest_header(chunk.metadata)

        if slide_matches:
            # PowerPoint presentations track slide numbers
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
            # Paginated documents (PDF, Word) track physical page breaks
            page_num = 1 + sum(1 for pb in page_breaks if pb < idx)
            chunk.metadata["page_number"] = page_num
            chunk.metadata["locator"] = f"Page {page_num}"
        else:
            # Unpaginated documents (Excel, Web, Plain Markdown) rely on section headers
            chunk.metadata.pop("page_number", None)
            chunk.metadata["locator"] = deepest_header or meta.get("file_name", "Document")

        # 5. Sanitize internal locator tags from chunk page_content
        chunk.page_content = re.sub(
            r"<!--\s*(?:page\s*break|Slide\s*number:\s*\d+)\s*-->\s*",
            "\n\n",
            chunk.page_content,
            flags=re.IGNORECASE,
        ).strip()

    # Filter out empty ghost chunks that only contained stripped locator tags
    return [c for c in chunks if c.page_content]


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
