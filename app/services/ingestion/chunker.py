"""Split cleaned Markdown by page, heading, and size while preserving source locators."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal, NamedTuple

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.config.config import get_settings

__all__ = ["build_chunk_context", "markdown_chunker", "recursive_chunker"]

_LOCATOR_TAG_PATTERN = re.compile(
    r"<!--\s*(?:(Slide|Page)\s*number:\s*(\d+)|page\s*break)\s*-->",
    flags=re.IGNORECASE,
)
_FENCE_REGEX = re.compile(r"^(`{3,}|~{3,})(.*)$")

# A markdown table: header row, separator row, then zero or more data rows.
_TABLE_BLOCK = re.compile(
    r"^[ \t]*\|.*\|[ \t]*\r?\n"
    r"[ \t]*\|[ \t:|-]*-[ \t:|-]*\r?\n?"
    r"(?:[ \t]*\|.*\|[ \t]*(?:\r?\n|$))*",
    flags=re.MULTILINE,
)


class PageSection(NamedTuple):
    """Source content and its explicit or sequential page/slide locator."""

    text: str
    page_number: int | None
    slide_number: int | None
    locator_type: Literal["page", "slide", "unpaginated"]


def _get_deepest_header(metadata: dict[str, Any], ordered_keys: list[str]) -> str | None:
    """Return the deepest available markdown header from metadata."""
    return next((metadata[k] for k in reversed(ordered_keys) if metadata.get(k)), None)


def _build_section_path(metadata: dict[str, Any], ordered_keys: list[str]) -> str:
    """Build a hierarchical breadcrumb string from the shallowest to the deepest header."""
    return " > ".join(metadata[k] for k in ordered_keys if metadata.get(k))


def _inherit_headers(
    metadata: dict[str, Any],
    inherited: dict[str, str],
    ordered_keys: list[str],
) -> None:
    """Fill missing headers from `inherited`, but only until the chunk defines its own header.

    Once a chunk has its own header at some level, deeper inherited headers are stale
    and must not leak into it.
    """
    overridden = False
    for key in ordered_keys:
        if key in metadata:
            overridden = True
        elif not overridden and key in inherited:
            metadata[key] = inherited[key]


def _fenced_ranges(text: str) -> list[tuple[int, int]]:
    """Return the character ranges covered by fenced code blocks."""
    ranges: list[tuple[int, int]] = []
    fence: str | None = None
    start = 0
    offset = 0

    for line in text.splitlines(keepends=True):
        match = _FENCE_REGEX.match(line.strip())
        if match:
            marker = match.group(1)
            if fence is None:
                if marker[0] == "`" and "`" in match.group(2):
                    offset += len(line)
                    continue
                fence = marker
                start = offset
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not match.group(2).strip():
                ranges.append((start, offset + len(line)))
                fence = None
        offset += len(line)

    if fence is not None:
        ranges.append((start, len(text)))
    return ranges


def _split_into_sections(markdown: str) -> list[PageSection]:
    """Split Markdown into page/slide sections at locator markers.

    Numbered markers (``Page number: N`` / ``Slide number: N``) use their explicit
    number. Unnumbered ``page break`` markers are numbered sequentially, taking
    any text before the first marker into account. Markers inside fenced code
    blocks are ignored. With no locator markers, the whole input is one
    ``"unpaginated"`` section.
    """
    fences = _fenced_ranges(markdown)
    matches = [
        match
        for match in _LOCATOR_TAG_PATTERN.finditer(markdown)
        if not any(start <= match.start() < end for start, end in fences)
    ]
    if not matches:
        return [PageSection(markdown, None, None, "unpaginated")]

    is_slide = any((m.group(1) or "").lower() == "slide" for m in matches)
    loc_type: Literal["page", "slide"] = "slide" if is_slide else "page"
    sections: list[PageSection] = []

    # Count leading unnumbered page breaks occurring before any substantive text
    first_marker_pos = matches[0].start()
    pre_text = markdown[:first_marker_pos].strip()

    leading_page_breaks = 0
    if not pre_text and not is_slide:
        # Check how many unnumbered page breaks are at the very beginning before first text
        for m in matches:
            if m.group(2) is None:  # unnumbered page break
                leading_page_breaks += 1
            else:
                break

    page_offset = 0
    if pre_text:
        first_num = matches[0].group(2)
        num = int(first_num) if first_num else 1
        sections.append(PageSection(pre_text, num, num if is_slide else None, loc_type))
        page_offset = 1
    elif leading_page_breaks > 0:
        page_offset = leading_page_breaks

    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        block = markdown[match.end() : end].strip()
        if not block:
            continue

        tag_type = (match.group(1) or "").lower()
        tag_num = match.group(2)
        num = int(tag_num) if tag_num else (
            1 + page_offset + (idx - leading_page_breaks + 1 if not pre_text and leading_page_breaks > 0 else idx)
        )

        if tag_type == "slide" or (not tag_type and is_slide):
            sections.append(PageSection(block, num, num, "slide"))
        else:
            sections.append(PageSection(block, num, None, "page"))

    return sections


def _apply_locator(
    metadata: dict[str, Any],
    section: PageSection,
    ordered_keys: list[str],
    default_label: str,
) -> None:
    """Write page/slide/locator fields into chunk metadata.

    Slides also set ``page_number`` so downstream citations can use a single
    numeric field for both PDF pages and presentation slides.
    """
    if section.locator_type == "slide" and section.slide_number is not None:
        n = section.slide_number
        metadata.update(slide_number=n, page_number=n, locator=f"Slide {n}")
    elif section.locator_type == "page" and section.page_number is not None:
        n = section.page_number
        metadata.update(page_number=n, locator=f"Page {n}")
    else:
        metadata.pop("page_number", None)
        metadata["locator"] = _get_deepest_header(metadata, ordered_keys) or default_label


def markdown_chunker(
    markdown: str,
    metadata: dict[str, Any] | None = None,
    headers_to_split_on: list[tuple[str, str]] | None = None,
) -> list[Document]:
    """Split normalized Markdown by page and heading, retaining source headings.

    Chunks inherit active parent headers across pages and receive section_path
    and locator metadata. Heading normalization belongs to clean_markdown.
    """
    settings = get_settings()
    headers = headers_to_split_on or settings.headers_to_split_on
    base_meta = metadata or {}

    # Map header depth -> metadata key, ordered from shallowest to deepest
    levels = {len(marker): key for marker, key in headers}
    ordered_keys = [levels[depth] for depth in sorted(levels)]
    default_label = base_meta.get("file_name", "Document")

    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers, strip_headers=False)

    output_chunks: list[Document] = []
    carried_headers: dict[str, str] = {}

    for section in _split_into_sections(markdown):
        page_text = section.text.strip()
        if not page_text:
            continue

        start_headers = carried_headers
        # Protect complete fences: the library handles only triple fences and
        # otherwise strips indentation from source code.
        code_blocks: dict[str, str] = {}
        for start, end in reversed(_fenced_ranges(page_text)):
            placeholder = uuid.uuid4().hex
            code_blocks[placeholder] = page_text[start:end].rstrip("\n")
            page_text = page_text[:start] + placeholder + "\n" + page_text[end:]
        page_chunks = splitter.split_text(page_text)

        for chunk in page_chunks:
            for placeholder, code in code_blocks.items():
                chunk.page_content = chunk.page_content.replace(placeholder, code)
            meta = chunk.metadata

            _inherit_headers(meta, start_headers, ordered_keys)

            chunk.metadata = meta = {**base_meta, **meta}

            section_path = _build_section_path(meta, ordered_keys)
            if section_path:
                meta["section_path"] = section_path
            _apply_locator(meta, section, ordered_keys, default_label)

            chunk.page_content = chunk.page_content.strip()
            if chunk.page_content:
                output_chunks.append(chunk)

        if page_chunks:
            carried_headers = {
                key: page_chunks[-1].metadata[key]
                for key in ordered_keys
                if key in page_chunks[-1].metadata
            }

    return output_chunks


def _split_table_rows(table_text: str, max_chunk_size: int) -> list[str]:
    """Pack consecutive source rows without inserting repeated headers."""
    lines = [line.strip() for line in table_text.splitlines() if line.strip()]
    chunks: list[str] = []
    rows: list[str] = []
    length = 0

    for row in lines:
        row_len = len(row) + (1 if rows else 0)
        if rows and length + row_len > max_chunk_size:
            chunks.append("\n".join(rows))
            rows, length = [], 0
            row_len = len(row)
        rows.append(row)
        length += row_len

    if rows:
        chunks.append("\n".join(rows))

    return chunks


def _segment_tables(text: str) -> list[tuple[str, bool]]:
    """Separate Markdown tables from surrounding text.

    Tables inside fenced code blocks are left untouched.

    Returns:
        List of (segment_text, is_table).
    """
    segments: list[tuple[str, bool]] = []
    cursor = 0
    fences = _fenced_ranges(text)

    for match in _TABLE_BLOCK.finditer(text):
        if any(start <= match.start() < end for start, end in fences):
            continue
        if text[cursor : match.start()].strip():
            segments.append((text[cursor : match.start()], False))
        segments.append((match.group(0), True))
        cursor = match.end()

    if text[cursor:].strip():
        segments.append((text[cursor:], False))

    return segments


def _split_chunk(
    doc: Document,
    splitter: RecursiveCharacterTextSplitter,
    chunk_size: int,
) -> list[Document]:
    """Split source content, keeping table context in metadata rather than repeated text."""
    text = doc.page_content
    if "|" not in text or len(text) <= chunk_size:
        return splitter.split_documents([doc])

    chunks: list[Document] = []
    for segment, is_table in _segment_tables(text):
        if is_table:
            table_header = "\n".join(segment.strip().splitlines()[:2])
            for part in _split_table_rows(segment, chunk_size):
                sub_doc = Document(
                    page_content=part,
                    metadata={**doc.metadata, "table_header": table_header},
                )
                chunks.extend(splitter.split_documents([sub_doc]))
        else:
            sub_doc = Document(page_content=segment, metadata=dict(doc.metadata))
            chunks.extend(splitter.split_documents([sub_doc]))

    return chunks


def recursive_chunker(
    documents: list[Document],
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    separators: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[Document]:
    """Bound source chunks by character count without merging across input chunks.

    Tables are packed by row, with oversized rows split recursively. Optional
    metadata is applied to the resulting chunks for compatibility with callers.
    """
    settings = get_settings()
    size = chunk_size if chunk_size is not None else settings.chunk_size
    overlap = chunk_overlap if chunk_overlap is not None else settings.chunk_overlap
    seps = list(separators if separators is not None else settings.separators)
    if "" not in seps:
        seps.append("")

    splitter = RecursiveCharacterTextSplitter(
        separators=seps,
        chunk_size=size,
        chunk_overlap=overlap,
        keep_separator="end",
    )

    raw_chunks: list[Document] = []
    for doc in documents:
        raw_chunks.extend(_split_chunk(doc, splitter, size))

    if metadata:
        for chunk in raw_chunks:
            chunk.metadata.update(metadata)
    return raw_chunks


def build_chunk_context(content: str, metadata: Mapping[str, Any]) -> str:
    """Add retrieval context without changing the source used for citations."""
    parts: list[str] = []
    section_path = metadata.get("section_path")
    if section_path:
        parts.append(f"Section: {section_path}")
    table_header = metadata.get("table_header")
    if table_header and not content.lstrip().startswith(table_header):
        parts.append(table_header)
    return "\n".join([*parts, content])
