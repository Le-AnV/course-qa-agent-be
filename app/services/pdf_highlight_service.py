"""Render a cited PDF with the passages used for an answer highlighted."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pymupdf

NGRAM = 3
MIN_MATCH_RUN = 4
MAX_SENTENCE_EXPANSION_WORDS = 25
SENTENCE_TERMINATORS = (".", "!", "?", ";", "。", "！", "？")


def _norm(word: str) -> str:
    return re.sub(r"\W+", "", word).lower()


def _tokens(text: str) -> list[str]:
    """Normalized words of a passage; markdown markers and punctuation vanish."""
    return [token for token in (_norm(word) for word in text.split()) if token]


def _ngrams(tokens: Sequence[str]) -> set[tuple[str, ...]]:
    return {tuple(tokens[i : i + NGRAM]) for i in range(len(tokens) - NGRAM + 1)}


def _is_sentence_end(text: str) -> bool:
    """Check if word text ends with sentence-terminating punctuation."""
    clean = text.strip()
    return bool(clean and clean[-1] in SENTENCE_TERMINATORS)


def _compute_page_rects(
    page: pymupdf.Page,
    passages: Sequence[str],
) -> list[pymupdf.Rect]:
    """Compute highlight bounding boxes for passages with sentence expansion and noise filtering."""
    if not passages:
        return []

    grams: set[tuple[str, ...]] = set()
    max_passage_len = 0
    for passage in passages:
        toks = _tokens(passage)
        if toks:
            max_passage_len = max(max_passage_len, len(toks))
            grams |= _ngrams(toks)

    if not grams:
        return []

    words = page.get_text("words")  # (x0, y0, x1, y1, text, block, line, n)
    if not words:
        return []

    normalized = [_norm(word[4]) for word in words]
    total_words = len(words)
    raw_marked = [False] * total_words

    # 1. Match n-grams
    for start in range(total_words - NGRAM + 1):
        if tuple(normalized[start : start + NGRAM]) in grams:
            for offset in range(NGRAM):
                raw_marked[start + offset] = True

    # 2. Bridge 1-2 word gaps within a passage
    for index in range(1, total_words - 1):
        if not raw_marked[index] and raw_marked[index - 1] and raw_marked[index + 1]:
            raw_marked[index] = True

    # 3. Filter isolated false positives (e.g. random 3-word verb phrases)
    # If the original passage was substantial, ignore isolated runs shorter than MIN_MATCH_RUN
    effective_min_run = min(MIN_MATCH_RUN, max_passage_len) if max_passage_len > 0 else MIN_MATCH_RUN
    filtered_marked = [False] * total_words

    i = 0
    valid_runs: list[tuple[int, int]] = []
    while i < total_words:
        if raw_marked[i]:
            run_start = i
            while i < total_words and raw_marked[i]:
                i += 1
            run_end = i
            if (run_end - run_start) >= effective_min_run:
                valid_runs.append((run_start, run_end))
        else:
            i += 1

    if not valid_runs:
        return []

    # 4. Expand valid runs to coherent sentence boundaries so users see full meaningful assertions
    for run_start, run_end in valid_runs:
        block_idx = words[run_start][5]

        # Expand backwards to start of sentence or block
        sent_start = run_start
        expansion_count = 0
        while sent_start > 0 and expansion_count < MAX_SENTENCE_EXPANSION_WORDS:
            prev_word = words[sent_start - 1]
            if prev_word[5] != block_idx:  # Crossed block boundary
                break
            if _is_sentence_end(prev_word[4]):  # Hit end of preceding sentence
                break
            sent_start -= 1
            expansion_count += 1

        # Expand forward to end of sentence or block
        sent_end = run_end
        expansion_count = 0
        while sent_end < total_words and expansion_count < MAX_SENTENCE_EXPANSION_WORDS:
            curr_word = words[sent_end - 1]
            if _is_sentence_end(curr_word[4]):  # Finished current sentence
                break
            if sent_end < total_words and words[sent_end][5] != block_idx:
                break
            sent_end += 1
            expansion_count += 1

        for idx in range(sent_start, sent_end):
            filtered_marked[idx] = True

    rects = [pymupdf.Rect(word[:4]) for word, hit in zip(words, filtered_marked, strict=True) if hit]
    return rects


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
