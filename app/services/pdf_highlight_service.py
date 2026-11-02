"""Render a cited PDF with the passages used for an answer highlighted."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import pymupdf

from app.services.ingestion.chunker import _fenced_ranges
from app.services.ingestion.cleaner import _protect, _restore, _strip_emphasis

_TOKEN_PATTERN = re.compile(r"\w+(?:\.\w+)*|[^\w\s]")


def _norm(word: str) -> str:
    return unicodedata.normalize("NFC", word).replace("\u00ad", "")


def _tokens(text: str) -> list[str]:
    """Normalize source formatting without discarding signs, operators, or identifiers."""
    text = text.strip()
    fences = _fenced_ranges(text)
    if fences:
        tokens: list[str] = []
        cursor = 0
        for start, end in fences:
            tokens.extend(_tokens(text[cursor:start]))
            lines = text[start:end].splitlines()
            opening = re.match(r"`{3,}|~{3,}", lines[0].lstrip())[0]
            body = lines[1:]
            if body and re.fullmatch(rf"{re.escape(opening[0])}{{{len(opening)},}}", body[-1].strip()):
                body.pop()
            tokens.extend(_TOKEN_PATTERN.findall(_norm("\n".join(body))))
            cursor = end
        tokens.extend(_tokens(text[cursor:]))
        return tokens
    text, blocks = _protect(text)
    # PDF extraction adds these formatting tags; they are not visible source text.
    text = re.sub(r"</?(?:u|mark|sup|sub)>", "", text)
    text = re.sub(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|[-+*][ \t]+)", "", text)
    text = _strip_emphasis(text)
    if text.startswith("|") and text.endswith("|"):
        text = " ".join(re.split(r"(?<!\\)\|", text[1:-1])).replace(r"\|", "|")
    text = _restore(text, blocks).replace("`", "")
    return _TOKEN_PATTERN.findall(_norm(text))


def _compute_page_rects(
    page: pymupdf.Page,
    passages: Sequence[str],
) -> list[pymupdf.Rect]:
    """Match each full passage at one unique position, without sentence expansion."""
    if not passages:
        return []

    words = page.get_text("words", sort=True)  # (x0, y0, x1, y1, text, block, line, n)
    if not words:
        return []

    variants: list[tuple[list[str], list[tuple[int, ...]]]] = []
    for dehyphenate in (False, True):
        tokens: list[str] = []
        spans: list[tuple[int, ...]] = []
        index = 0
        while index < len(words):
            text = _norm(words[index][4])
            indexes = (index,)
            if text in {"•", "·", "\uf0b7"} and words[index][7] == 0:
                index += 1
                continue
            if (
                dehyphenate and index + 1 < len(words)
                and len(text) > 1 and text.endswith("-") and text[-2].isalpha()
                and words[index][5] == words[index + 1][5]
                and words[index][6] != words[index + 1][6]
                and words[index + 1][4][0].islower()
            ):
                text = text[:-1] + _norm(words[index + 1][4])
                indexes = (index, index + 1)
                index += 1
            parts = _TOKEN_PATTERN.findall(text)
            tokens.extend(parts)
            spans.extend([indexes] * len(parts))
            index += 1
        variants.append((tokens, spans))

    selected_words: set[int] = set()
    for passage in passages:
        expected = _tokens(passage)
        if not expected:
            continue
        matches: set[tuple[int, ...]] = set()
        for tokens, spans in variants:
            for start in range(len(tokens) - len(expected) + 1):
                if tokens[start:start + len(expected)] == expected:
                    end = start + len(expected)
                    if (
                        (start > 0 and spans[start - 1] == spans[start])
                        or (end < len(spans) and spans[end - 1] == spans[end])
                    ):
                        continue
                    matches.add(tuple(sorted({
                        index for span in spans[start:end] for index in span
                    })))
        if len(matches) == 1:
            selected_words.update(next(iter(matches)))
    return [pymupdf.Rect(words[index][:4]) for index in sorted(selected_words)]


def get_highlight_rects(
    path: Path,
    page_number: int,
    passages: Sequence[str],
) -> list[dict[str, float]]:
    """Return lightweight bounding boxes [{x0, y0, x1, y1}] without re-rendering entire PDF bytes."""
    doc = pymupdf.open(path)
    try:
        if 1 <= page_number <= doc.page_count:
            page = doc[page_number - 1]
            rects = _compute_page_rects(page, passages)
            return [
                {"x0": round(r.x0, 2), "y0": round(r.y0, 2), "x1": round(r.x1, 2), "y1": round(r.y1, 2)}
                for r in rects
            ]
        return []
    finally:
        doc.close()


def highlight_pdf(path: Path, page_number: int, passages: Sequence[str]) -> bytes:
    """Return the PDF with the cited passages highlighted on ``page_number``."""
    doc = pymupdf.open(path)
    try:
        if 1 <= page_number <= doc.page_count:
            page = doc[page_number - 1]
            rects = _compute_page_rects(page, passages)
            if rects:
                annotation = page.add_highlight_annot(quads=[rect.quad for rect in rects])
                annotation.set_colors(stroke=(1, 0.85, 0.2))
                annotation.update()
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()
