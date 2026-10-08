"""Text cleaner and preprocessing module."""

import os
import re
import unicodedata
from typing import Dict, List, Optional, Tuple


def protect_special_blocks(text: str) -> Tuple[str, Dict[str, str]]:
    """Extract and protect code blocks, math blocks, and markdown tables.

    This prevents text-based regular expressions from accidentally altering
    code syntax, mathematical formulas, or table structures.

    Args:
        text (str): The original markdown text.

    Returns:
        Tuple[str, Dict[str, str]]: A tuple containing the text with placeholders
        and a dictionary mapping placeholders to the original blocks.
    """
    placeholders = {}
    counter = 0

    def replacer(match: re.Match) -> str:
        nonlocal counter
        key = f"__PROTECTED_BLOCK_{counter}__"
        placeholders[key] = match.group(0)
        counter += 1
        return key

    # Protect Fenced Code Blocks (```...```)
    text = re.sub(r"```[\s\S]*?```", replacer, text)
    
    # Protect Inline Code (`...`)
    text = re.sub(r"`[^`\n]+`", replacer, text)
    
    # Protect Block Math ($$...$$)
    text = re.sub(r"\$\$[\s\S]*?\$\$", replacer, text)

    return text, placeholders


def restore_special_blocks(text: str, placeholders: Dict[str, str]) -> str:
    """Restore protected blocks into their original placeholders.

    Args:
        text (str): The text containing placeholders.
        placeholders (Dict[str, str]): The mapping of placeholders to original text.

    Returns:
        str: The restored markdown text.
    """
    for key, val in placeholders.items():
        text = text.replace(key, val)
    return text


def clean_page_artifacts(text: str, custom_watermarks: Optional[List[str]] = None) -> str:
    """Remove page numbers, page break tags, and watermarks.

    Args:
        text (str): The markdown text.
        custom_watermarks (Optional[List[str]]): List of specific watermark strings to remove.

    Returns:
        str: The cleaned text.
    """
    # Remove lines containing only page numbers (e.g., "Page 1", "- 12 -", "12")
    text = re.sub(
        r"^(?:[-_]*page\s*\d+[-_]*|[-_]+\s*\d+\s*[-_]+|\s*\d{1,4}\s*)$",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE
    )

    if custom_watermarks:
        for wm in custom_watermarks:
            pattern = rf"^\s*{re.escape(wm)}\s*$"
            text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)
            
    return text


def clean_unicode_and_ligatures(text: str) -> str:
    """Normalize Unicode and replace common typographical ligatures.

    - Applies NFC normalization.
    - Replaces common PDF ligatures (fi, fl, ff) and smart quotes.
    - Removes zero-width and invisible control characters.

    Args:
        text (str): The markdown text.

    Returns:
        str: The normalized text.
    """
    text = unicodedata.normalize("NFC", text)

    ligatures_map = {
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
    for bad_char, good_char in ligatures_map.items():
        text = text.replace(bad_char, good_char)

    # Remove zero-width spaces and specific non-printable characters
    text = re.sub(r"[\u200b\u200c\u200d\uFEFF]", "", text)

    return text


def clean_hyphenation_and_wrapping(text: str) -> str:
    """Fix broken words and hard line wraps within sentences.

    Supports both English and Vietnamese characters.

    Args:
        text (str): The markdown text.

    Returns:
        str: The text with repaired word breaks and wraps.
    """
    # Rejoin words split across lines by a hyphen
    text = re.sub(r"(\b[\w\u00C0-\u1EF9]+)-\s*\n\s*([\w\u00C0-\u1EF9]+\b)", r"\1\2", text)

    # Rejoin hard line breaks within a sentence (allows next line to start with uppercase for proper nouns)
    text = re.sub(r"([a-z\u00C0-\u1EF9,])\n([A-Za-z\u00C0-\u1EF9])", r"\1 \2", text)

    return text


def clean_markdown_formatting(text: str) -> str:
    """Standardize markdown formatting and spacing.

    - Ensures headings have a space after the hash symbol.
    - Removes trailing whitespace from lines.
    - Cleans up HTML tags inside markdown tables.
    - Collapses three or more consecutive newlines into two.

    Args:
        text (str): The markdown text.

    Returns:
        str: The standardized markdown text.
    """
    # Fix headers missing a space (e.g., "##Header" -> "## Header")
    text = re.sub(r"^(#{1,6})([^\s#])", r"\1 \2", text, flags=re.MULTILINE)

    # Remove trailing whitespace
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)

    # Clean up <br> tags within markdown tables
    lines = text.splitlines()
    cleaned_lines = []
    for line in lines:
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cleaned_line = re.sub(r"<br\s*/?>", " ", line)
            cleaned_lines.append(cleaned_line)
        else:
            cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)

    # Collapse multiple newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def clean_latex_math(text: str) -> str:
    """Optimize LaTeX mathematical formulas.

    - Merges fragmented inline math blocks separated by operators.
    - Strips leading and trailing whitespace inside inline math tags.

    Args:
        text (str): The markdown text.

    Returns:
        str: The text with optimized math formulas.
    """
    # Merge consecutive inline math blocks (handles chains like $a$ + $b$ = $c$)
    pattern = r"\$\s*([^\$\n]+?)\s*\$\s*([\+\-\*\/=><])\s*\$\s*([^\$\n]+?)\s*\$"
    while re.search(pattern, text):
        text = re.sub(pattern, r"$\1 \2 \3$", text)
    
    # Remove padding spaces inside inline math tags
    text = re.sub(r"\$\s+([^\$\n]+?)\s+\$", r"$\1$", text)

    return text


def clean_image_tags(text: str, base_image_dir: Optional[str] = None) -> str:
    """Remove orphaned image tags and their captions.

    Args:
        text (str): The markdown text.
        base_image_dir (Optional[str]): The local directory where images are stored.
            If provided, verifies if the image file exists.

    Returns:
        str: The text with invalid images removed.
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
    text = re.sub(r"^\s*(?:Figure|Fig\.|Hình)\s*\d+[\s:.-]*$", "", text, flags=re.IGNORECASE | re.MULTILINE)

    return text


def clean_marker_markdown(markdown_text: str, watermarks: Optional[List[str]] = None) -> str:
    """Execute the full text cleaning pipeline on parsed markdown.

    Args:
        markdown_text (str): The raw markdown text generated by a parser.
        watermarks (Optional[List[str]]): A list of watermark strings to remove.

    Returns:
        str: The fully cleaned and standardized markdown text.
    """
    text, placeholders = protect_special_blocks(markdown_text)

    text = clean_unicode_and_ligatures(text)
    text = clean_page_artifacts(text, custom_watermarks=watermarks)
    text = clean_hyphenation_and_wrapping(text)
    text = clean_latex_math(text)

    text = restore_special_blocks(text, placeholders)

    text = clean_markdown_formatting(text)

    return text