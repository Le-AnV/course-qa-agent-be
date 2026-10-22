"""Markdown cleaner for ingestion pipelines.

Normalizes Markdown extracted from PDF, Word, PowerPoint, and text loaders before
chunking and embedding. Code, math, and HTML comments (locators such as
`<!-- Slide number: 1 -->` or `<!-- page break -->`) are protected and restored untouched.
"""

import re
import unicodedata

# Blocks that must never be modified during text normalization.
# A single alternation ensures a single pass: the leftmost match wins,
# so "$" inside code or backticks inside HTML comments are never misread.
_PROTECTED = re.compile(
    r"""
      (?P<fence>`{3,}|~{3,})[\s\S]*?(?P=fence)   # Fenced code blocks
    | `[^`\n]+`                                  # Inline code
    | <!--[\s\S]*?-->                            # HTML comments / locator tags
    | \$\$[\s\S]*?\$\$                           # Block math
    | \$(?=\S)[^$\n]*?(?<=\S)\$(?!\d)            # Inline math (excludes currency like "$5 and $10")
    """,
    re.VERBOSE,
)
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")

# Character translation table:
# Non-breaking space -> standard space; zero-width spaces and BOM -> removed.
_CHAR_MAP = str.maketrans({"\u00a0": " ", "\u200b": None, "\ufeff": None})

# Standalone lines that represent only a page number:
# e.g., "Page 3", "Trang 3/10", "- 12 -", "12".
_PAGE_NUMBER = re.compile(
    r"""^[ \t]*(?:
        [-–—_]*[ \t]*(?:page|trang)[ \t]*\d+(?:[ \t]*(?:of|/)[ \t]*\d+)?[ \t]*[-–—_]*
      | [-–—_]+[ \t]*\d+[ \t]*[-–—_]+
      | \d{1,4}
    )[ \t]*$\n?""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Soft hyphenation break: "infor-\nmation" -> "information".
# Requires letters on both sides, a single line break, and ensures the next word
# does not start with an uppercase ASCII letter to avoid joining proper nouns or abbreviations
# (e.g. keeps "COVID-\n19" and "Bà Rịa-\nVũng Tàu" separate).
_HYPHEN_BREAK = re.compile(r"(?<=[^\W\d_])-[ \t]*\n[ \t]*(?=[^\W\d_A-Z])")

# Soft-wrapped sentences: line ends with a letter, digit, or comma, and the next line
# starts with a lowercase letter. Headings, list items, quotes, and table rows are preserved.
_LINE_WRAP = re.compile(
    r"^(?![ \t]*(?:#|[-*+>|]|\d+[.)][ \t]))(.*[\w,])\n(?=[^\W\d_A-Z])",
    re.MULTILINE,
)

_HEADING = re.compile(r"^(#{1,6})(?=[^\s#])", re.MULTILINE)  # Standardizes "##Title" -> "## Title"
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_LINES = re.compile(r"\n{3,}")


def _protect(text: str) -> tuple[str, list[str]]:
    """Replace protected code, math, and locator blocks with NUL-delimited placeholders."""
    blocks: list[str] = []

    def stash(match: re.Match) -> str:
        blocks.append(match[0])
        return f"\x00{len(blocks) - 1}\x00"

    return _PROTECTED.sub(stash, text), blocks


def _restore(text: str, blocks: list[str]) -> str:
    """Restore all protected blocks back to their original text in a single pass."""
    return _PLACEHOLDER.sub(lambda m: blocks[int(m[1])], text) if blocks else text


def clean_markdown(
    markdown_text: str,
    watermarks: list[str] | None = None,
    rejoin_line_wraps: bool = False,
) -> str:
    """Clean and standardize raw Markdown extracted from any document loader.

    Pipeline:
    1. Line-ending normalization (CRLF/CR -> LF) and NUL-byte stripping.
    2. Protection of sensitive blocks (code fences, inline code, HTML locators, math).
    3. Invisible character cleanup and Unicode NFC normalization (essential for Vietnamese diacritics).
    4. Stripping of isolated page numbers and custom watermark lines.
    5. Repair of soft-hyphenated words and optional rejoining of soft line wraps.
    6. Markdown syntax standardization (heading spacing, trailing whitespace, blank line collapse).
    7. Single-pass restoration of protected blocks.

    Args:
        markdown_text: Raw Markdown string extracted from PDF, Word, PPTX, Excel, or Text loader.
        watermarks: Optional list of exact watermark lines (case-insensitive) to strip.
        rejoin_line_wraps: If True, merges hard line breaks inside ongoing sentences.
            Defaults to False so bullet points, slide lines, and poetry remain intact.

    Returns:
        Cleaned, normalized Markdown string ready for semantic chunking and embedding.
    """
    if not markdown_text or not markdown_text.strip():
        return ""

    # 1. Normalize line endings; strip NULs to safely use \x00 as placeholder delimiter.
    text = markdown_text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")

    # 2. Protect code blocks, math expressions, and HTML comment locators.
    text, blocks = _protect(text)

    # 3. Clean control characters and normalize Unicode to NFC format.
    text = unicodedata.normalize("NFC", text.translate(_CHAR_MAP))

    # 4. Remove standalone page number artifacts and watermarks.
    text = _PAGE_NUMBER.sub("", text)
    if watermarks:
        valid_watermarks = [w.strip() for w in watermarks if w and w.strip()]
        if valid_watermarks:
            alternatives = "|".join(map(re.escape, valid_watermarks))
            text = re.sub(
                rf"^[ \t]*(?:{alternatives})[ \t]*$\n?",
                "",
                text,
                flags=re.IGNORECASE | re.MULTILINE,
            )

    # 5. Fix hyphenation breaks and optionally rejoin wrapped sentence lines.
    text = _HYPHEN_BREAK.sub("", text)
    if rejoin_line_wraps:
        text = _LINE_WRAP.sub(r"\1 ", text)

    # 6. Normalize Markdown syntax (heading spacing, trailing whitespace, consecutive newlines).
    text = _HEADING.sub(r"\1 ", text)
    text = _TRAILING_WS.sub("", text)
    text = _BLANK_LINES.sub("\n\n", text).strip()

    # 7. Restore all protected blocks untouched.
    return _restore(text, blocks)


# Backwards compatibility alias for existing loader integrations
clean_marker_markdown = clean_markdown

__all__ = ["clean_markdown", "clean_marker_markdown"]
