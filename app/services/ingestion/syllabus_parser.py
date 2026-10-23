"""Syllabus Markdown parser for course units and topic outline extraction (AI-133).

Extracts the high-level ``Topic Outline`` and sequential course units (1..N)
with structured session/outline content from syllabus Markdown produced by the
ingestion pipeline (from ``.xlsx``/``.xls`` or ``.md`` syllabus files).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class SyllabusFormatError(ValueError):
    """Raised when a syllabus document cannot be parsed into course units (AC-04)."""


@dataclass
class ParsedSyllabusUnit:
    unit_number: int
    unit_title: str
    description: str | None
    content: str


@dataclass
class SyllabusRoadmapData:
    topic_outline: str | None = None
    units: list[ParsedSyllabusUnit] = field(default_factory=list)


_DANGEROUS_TAG_BLOCK_RE = re.compile(
    r"<\s*(script|iframe|object|embed|style|applet|form|meta|link)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DANGEROUS_TAG_SINGLE_RE = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|style|applet|form|meta|link|svg|math|base)\b[^>]*>",
    re.IGNORECASE,
)
_EVENT_HANDLER_ATTR_RE = re.compile(
    r"\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
_JS_LINK_RE = re.compile(
    r"(\[[^\]]*\]\()\s*(?:javascript|vbscript|data)\s*:[^)]*(\))",
    re.IGNORECASE,
)
_RAW_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

_UNIT_HEADER_RE = re.compile(
    r"^(?:unit|chương|chapter|module)\s*0*(\d+)\s*[:.\-–—]\s*(.+)$",
    re.IGNORECASE,
)


def sanitize_markdown_content(text: str | None) -> str:
    """Sanitize Markdown outline content against XSS while preserving Markdown syntax (AC-07)."""
    if not text:
        return ""
    cleaned = _DANGEROUS_TAG_BLOCK_RE.sub("", text)
    cleaned = _DANGEROUS_TAG_SINGLE_RE.sub("", cleaned)
    cleaned = _EVENT_HANDLER_ATTR_RE.sub("", cleaned)
    cleaned = _JS_LINK_RE.sub(r"\1#\2", cleaned)
    cleaned = _RAW_HTML_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def is_syllabus_document(file_name: str, markdown: str) -> bool:
    """Return True when a document should be parsed as a course syllabus.

    Only structured syllabus files (.xlsx, .xls, .md) with syllabus in the name
    or explicit Training Schedule / Topic Outline headers are treated as syllabi (AC-01).
    Plain text (.txt) files are generic notes unless explicitly containing syllabus headers.
    """
    lower_name = (file_name or "").lower()
    ext = ""
    if "." in lower_name:
        ext = "." + lower_name.rsplit(".", 1)[-1]

    has_training_schedule = bool(
        re.search(r"^#\s+Training\s+Schedule\b", markdown or "", re.IGNORECASE | re.MULTILINE)
    )
    has_topic_outline = bool(
        re.search(r"^##\s+Topic\s+Outline\b", markdown or "", re.IGNORECASE | re.MULTILINE)
    )
    if has_training_schedule or has_topic_outline:
        return True

    return bool("syllabus" in lower_name and ext in {".xlsx", ".xls", ".md"})


def _extract_topic_outline(markdown: str) -> str | None:
    """Extract raw text under ``## Topic Outline`` up to the next heading."""
    lines = markdown.splitlines()
    in_section = False
    collected: list[str] = []

    for line in lines:
        stripped = line.strip()
        if re.match(r"^##\s+Topic\s+Outline\b", stripped, re.IGNORECASE):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("#"):
                break
            collected.append(line)

    raw = "\n".join(collected).strip()
    if not raw:
        return None
    sanitized = sanitize_markdown_content(raw)
    return sanitized or None


def _split_table_cells(row_line: str) -> list[str]:
    """Split a Markdown table row ``| a | b |`` into trimmed cell strings."""
    inner = row_line.strip().removeprefix("|").removesuffix("|")
    cells = re.split(r"(?<!\\)\|", inner)
    return [c.replace(r"\|", "|").strip() for c in cells]


def _is_table_divider(cells: list[str]) -> bool:
    return bool(cells) and all(not c or set(c) <= {"-", ":", " "} for c in cells)


def _format_cell_items(raw_content: str) -> list[str]:
    """Split semicolon-joined cell content from ``excel_loader`` into clean lines."""
    parts = [p.strip() for p in raw_content.split(";") if p.strip()]
    cleaned_parts: list[str] = []
    for part in parts:
        item = re.sub(r"^[-*+]\s*", "", part).strip()
        if item:
            cleaned_parts.append(item)
    return cleaned_parts


def _format_schedule_row(cells: list[str]) -> tuple[list[str], str | None]:
    """Convert one schedule table row into clean Markdown bullet lines and objective tag."""
    padded = cells + [""] * max(0, 6 - len(cells))
    content_col, objectives, delivery, duration, training_format, _notes = padded[:6]

    items = _format_cell_items(content_col)
    if not items:
        return [], None

    meta_badges = [m for m in (delivery, duration, training_format, objectives) if m]
    badge_str = f" ({' · '.join(meta_badges)})" if meta_badges else ""

    lines: list[str] = [f"- **{items[0]}**{badge_str}"]
    for sub in items[1:]:
        lines.append(f"  - {sub}")

    return lines, (objectives or None)


def _convert_unit_schedule_body(body_lines: list[str]) -> tuple[str, str | None]:
    """Convert raw ``# Training Schedule`` unit lines into clean Markdown and a short summary."""
    output_lines: list[str] = []
    objectives_seen: list[str] = []
    session_count = 0

    for raw_line in body_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue

        if stripped.startswith("### "):
            session_count += 1
            if output_lines and output_lines[-1] != "":
                output_lines.append("")
            output_lines.append(stripped)
            continue

        if stripped.startswith("|"):
            cells = _split_table_cells(stripped)
            if not cells or _is_table_divider(cells):
                continue
            if cells[0].lower() == "content" and len(cells) > 1 and "objective" in cells[1].lower():
                continue
            row_md, obj = _format_schedule_row(cells)
            if row_md:
                output_lines.extend(row_md)
            if obj:
                for token in [t.strip() for t in obj.split(",") if t.strip()]:
                    if token not in objectives_seen:
                        objectives_seen.append(token)
            continue

        output_lines.append(raw_line)

    content = sanitize_markdown_content("\n".join(output_lines))
    desc_parts: list[str] = []
    if session_count:
        desc_parts.append(f"{session_count} session{'s' if session_count > 1 else ''}")
    if objectives_seen:
        desc_parts.append(", ".join(objectives_seen))
    description = " · ".join(desc_parts) if desc_parts else None
    return content, description


def _parse_training_schedule_units(markdown: str) -> list[ParsedSyllabusUnit]:
    """Extract sequential units under ``# Training Schedule``."""
    lines = markdown.splitlines()
    in_schedule = False
    raw_units: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if re.match(r"^#\s+Training\s+Schedule\b", stripped, re.IGNORECASE):
            in_schedule = True
            continue

        if not in_schedule:
            continue

        # End of Training Schedule when hitting another H1 or Document History
        if stripped.startswith("# ") or re.match(
            r"^##\s+Document\s+History\b", stripped, re.IGNORECASE
        ):
            break

        if stripped.startswith("## "):
            if current_title is not None:
                raw_units.append((current_title, current_lines))
            current_title = stripped[3:].strip()
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        raw_units.append((current_title, current_lines))

    units: list[ParsedSyllabusUnit] = []
    for raw_title, body_lines in raw_units:
        clean_title = sanitize_markdown_content(raw_title)
        match = _UNIT_HEADER_RE.match(clean_title)
        if match:
            clean_title = match.group(2).strip()
        content, description = _convert_unit_schedule_body(body_lines)
        if clean_title and content:
            units.append(
                ParsedSyllabusUnit(
                    unit_number=len(units) + 1,
                    unit_title=clean_title,
                    description=sanitize_markdown_content(description) if description else None,
                    content=content,
                )
            )
    return units


def _parse_markdown_heading_units(markdown: str) -> list[ParsedSyllabusUnit]:
    """Fallback for direct Markdown syllabi with ``# / ## / ### Unit X: <Title>`` headings."""
    lines = markdown.splitlines()
    raw_units: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_level: int = 0
    current_lines: list[str] = []

    heading_re = re.compile(r"^(#{1,3})\s+(.+)$")

    for line in lines:
        stripped = line.strip()
        m = heading_re.match(stripped)
        if m:
            hashes, title_text = m.group(1), m.group(2).strip()
            level = len(hashes)
            unit_match = _UNIT_HEADER_RE.match(title_text)
            if unit_match:
                if current_title is not None:
                    raw_units.append((current_title, current_lines))
                current_title = unit_match.group(2).strip()
                current_level = level
                current_lines = []
                continue
            if current_title is not None and level <= current_level:
                raw_units.append((current_title, current_lines))
                current_title = None
                current_lines = []
                continue

        if current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        raw_units.append((current_title, current_lines))

    units: list[ParsedSyllabusUnit] = []
    for title, body_lines in raw_units:
        clean_title = sanitize_markdown_content(title)
        content, description = _convert_unit_schedule_body(body_lines)
        if clean_title and content:
            units.append(
                ParsedSyllabusUnit(
                    unit_number=len(units) + 1,
                    unit_title=clean_title,
                    description=sanitize_markdown_content(description) if description else None,
                    content=content,
                )
            )
    return units


def parse_syllabus_markdown(markdown: str) -> SyllabusRoadmapData:
    """Parse Topic Outline and sequential Course Units (1..N) from syllabus Markdown.

    Raises:
        SyllabusFormatError: If no valid course units can be extracted (AC-04).
    """
    if not markdown or not markdown.strip():
        raise SyllabusFormatError("Syllabus format is not recognized or contains no course units.")

    topic_outline = _extract_topic_outline(markdown)
    units = _parse_training_schedule_units(markdown)
    if not units:
        units = _parse_markdown_heading_units(markdown)

    if not units:
        raise SyllabusFormatError("Syllabus format is not recognized or contains no course units.")

    return SyllabusRoadmapData(topic_outline=topic_outline, units=units)

