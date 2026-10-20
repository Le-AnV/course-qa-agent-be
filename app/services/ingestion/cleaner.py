"""Markdown cleaner and text normalization module for ingestion pipelines.

Prepares and standardizes Markdown extracted from PDF, Word (.docx/.doc),
PowerPoint (.pptx/.ppt), and Text files before chunking and embedding.
"""

import os
import re
import unicodedata

# Common typographic ligatures and smart punctuation
LIGATURES_MAP = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\u201c": '"',
    "\u201d": '"',
    "\u2018": "'",
    "\u2019": "'",
    "\u2013": "-",
    "\u2014": " - ",
    "\u00a0": " ",
}


def protect_special_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Extract and protect code blocks, inline code, math formulas, and HTML comments.

    Prevents downstream regex cleaning from corrupting code syntax, mathematical expressions,
    or locator metadata tags (e.g., <!-- Slide number: 1 -->, <!-- page break -->).

    Args:
        text (str): The original markdown text.

    Returns:
        tuple[str, dict[str, str]]: A tuple containing the text with placeholders
        and a mapping of placeholders to original blocks.
    """
    placeholders: dict[str, str] = {}
    counter = 0

    def replacer(match: re.Match) -> str:
        nonlocal counter
        key = f"__PROTECTED_BLOCK_{counter}__"
        placeholders[key] = match.group(0)
        counter += 1
        return key

    # 1. Fenced Code Blocks (```...```)
    text = re.sub(r"```[\s\S]*?```", replacer, text)

    # 2. Inline Code (`...`)
    text = re.sub(r"`[^`\n]+`", replacer, text)

    # 3. HTML Comments / Locators (<!-- ... -->)
    text = re.sub(r"<!--[\s\S]*?-->", replacer, text)

    # 4. Block Math ($$...$$)
    text = re.sub(r"\$\$[\s\S]*?\$\$", replacer, text)

    # 5. Inline Math ($...$)
    text = re.sub(r"(?<!\$)\$(?!\$)[\s\S]+?(?<!\$)\$(?!\$)", replacer, text)

    return text, placeholders


def restore_special_blocks(text: str, placeholders: dict[str, str]) -> str:
    """Restore previously protected blocks from placeholders.

    Args:
        text (str): The text containing placeholders.
        placeholders (dict[str, str]): Mapping of placeholders to original block contents.

    Returns:
        str: Restored markdown text.
    """
    for key, val in placeholders.items():
        text = text.replace(key, val)
    return text


def clean_unicode_and_ligatures(text: str) -> str:
    """Normalize Unicode to NFC and resolve typography ligatures.

    - Applies Unicode NFC normalization (crucial for Vietnamese diacritics).
    - Replaces typographic ligatures (fi, fl, ffi, ffl) and smart quotes with standard ASCII equivalents.
    - Removes zero-width and invisible control characters.

    Args:
        text (str): The input text.

    Returns:
        str: Normalized text.
    """
    text = unicodedata.normalize("NFC", text)

    for bad_char, good_char in LIGATURES_MAP.items():
        text = text.replace(bad_char, good_char)

    # Strip zero-width spaces and invisible characters
    text = re.sub(r"[\u200b\u200c\u200d\uFEFF]", "", text)

    return text


def clean_page_artifacts(text: str, custom_watermarks: list[str] | None = None) -> str:
    """Remove header/footer artifacts such as standalone page numbers and watermarks.

    Note: Locator comments like <!-- page break --> or <!-- Slide number: X --> are
    protected before this function runs.

    Args:
        text (str): The input text.
        custom_watermarks (list[str] | None): Optional list of exact watermark lines to remove.

    Returns:
        str: Text with stray page numbers and watermarks removed.
    """
    # Remove lines containing only page numbers (e.g. "Page 1", "- 12 -", "12")
    text = re.sub(
        r"^(?:[-_]*page\s*\d+[-_]*|[-_]+\s*\d+\s*[-_]+|\s*\d{1,4}\s*)$",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    if custom_watermarks:
        for wm in custom_watermarks:
            pattern = rf"^\s*{re.escape(wm)}\s*$"
            text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)

    return text


def clean_hyphenation_and_wrapping(text: str, rejoin_line_wraps: bool = False) -> str:
    """Repair words broken across lines by a hyphen and optionally soft line wraps.

    Args:
        text (str): The input text.
        rejoin_line_wraps (bool): Whether to rejoin sentences broken across lines.
            Defaults to False to prevent merging intentional bullet points or slide lines.

    Returns:
        str: Text with repaired hyphenation.
    """
    # Rejoin words broken across line break by hyphen (e.g. "thông-\n tin" -> "thôngtin")
    text = re.sub(r"(\b[\w\u00C0-\u1EF9]+)-\s*\n\s*([\w\u00C0-\u1EF9]+\b)", r"\1\2", text)

    if rejoin_line_wraps:
        # Rejoin hard line breaks within a sentence
        text = re.sub(r"([a-z\u00C0-\u1EF9,])\n([A-Za-z\u00C0-\u1EF9])", r"\1 \2", text)

    return text


def clean_latex_math(text: str) -> str:
    """Optimize and merge fragmented LaTeX inline math formulas.

    Args:
        text (str): The input text.

    Returns:
        str: Text with optimized LaTeX formulas.
    """
    # Merge consecutive inline math blocks separated by basic operators ($a$ + $b$ = $c$ -> $a + b = c$)
    pattern = r"\$\s*([^\$\n]+?)\s*\$\s*([\+\-\*\/=><])\s*\$\s*([^\$\n]+?)\s*\$"
    while re.search(pattern, text):
        text = re.sub(pattern, r"$\1 \2 \3$", text)

    # Remove extra spaces inside inline math tags ($ x $ -> $x$)
    text = re.sub(r"\$\s+([^\$\n]+?)\s+\$", r"$\1$", text)

    return text


def clean_markdown_formatting(text: str) -> str:
    """Standardize markdown headings, table line breaks, and excessive blank lines.

    Args:
        text (str): The input text.

    Returns:
        str: Standardized markdown text.
    """
    # Ensure headings have a space after '#' (e.g. "##Header" -> "## Header")
    text = re.sub(r"^(#{1,6})([^\s#])", r"\1 \2", text, flags=re.MULTILINE)

    # Strip trailing whitespace on each line
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)

    # Clean up <br> tags within markdown tables to preserve cell spacing
    lines = text.splitlines()
    cleaned_lines = []
    for line in lines:
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cleaned_line = re.sub(r"<br\s*/?>", " ", line)
            cleaned_lines.append(cleaned_line)
        else:
            cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)

    # Collapse 3 or more consecutive newlines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def clean_image_tags(text: str, base_image_dir: str | None = None) -> str:
    """Remove orphaned image tags if corresponding image files do not exist.

    Args:
        text (str): The input text.
        base_image_dir (str | None): Directory containing extracted image files.

    Returns:
        str: Text with invalid image tags removed.
    """
    def check_image(match: re.Match) -> str:
        img_path = match.group(2).strip()
        if not img_path:
            return ""
        if base_image_dir:
            full_path = os.path.join(base_image_dir, img_path)
            if not os.path.exists(full_path):
                return ""
        return match.group(0)

    # Remove invalid markdown image tags
    text = re.sub(r"!\[(.*?)\]\((.*?)\)", check_image, text)

    # Remove orphaned standalone captions
    text = re.sub(
        r"^\s*(?:Figure|Fig\.|Hình)\s*\d+[\s:.-]*$", "", text, flags=re.IGNORECASE | re.MULTILINE
    )

    return text


def clean_markdown(
    markdown_text: str,
    watermarks: list[str] | None = None,
    rejoin_line_wraps: bool = False,
) -> str:
    """Execute the full normalization pipeline on raw Markdown extracted from any loader.

    Args:
        markdown_text (str): Raw Markdown text from PDF, Word, PPTX, or Text loader.
        watermarks (list[str] | None): Optional list of watermarks to strip.
        rejoin_line_wraps (bool): Whether to merge hard line breaks within sentences. Defaults to False.

    Returns:
        str: Cleaned, standardized Markdown ready for chunking and embedding.
    """
    if not markdown_text or not markdown_text.strip():
        return ""

    # Step 1: Protect code blocks, math expressions, and HTML comments (locators)
    text, placeholders = protect_special_blocks(markdown_text)

    # Step 2: Unicode & Typography normalization (NFC, ligatures, smart quotes)
    text = clean_unicode_and_ligatures(text)

    # Step 3: Remove page artifacts and watermarks
    text = clean_page_artifacts(text, custom_watermarks=watermarks)

    # Step 4: Repair hyphenation across line breaks
    text = clean_hyphenation_and_wrapping(text, rejoin_line_wraps=rejoin_line_wraps)

    # Step 5: Clean LaTeX math formatting
    text = clean_latex_math(text)

    # Step 6: Restore protected code, math, and locator blocks
    text = restore_special_blocks(text, placeholders)

    # Step 7: Clean Markdown syntax (heading spaces, table <br>, trailing spaces, newlines)
    text = clean_markdown_formatting(text)

    return text


# Backwards compatibility alias
clean_marker_markdown = clean_markdown

__all__ = [
    "clean_hyphenation_and_wrapping",
    "clean_image_tags",
    "clean_latex_math",
    "clean_markdown",
    "clean_markdown_formatting",
    "clean_marker_markdown",
    "clean_page_artifacts",
    "clean_unicode_and_ligatures",
    "protect_special_blocks",
    "restore_special_blocks",
]
