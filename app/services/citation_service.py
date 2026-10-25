"""Citations helper service for deep-linking and text fragment generation."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Sequence
from typing import Any


def _clean_snippet(text: str) -> str:
    """Strip markdown formatting, HTML tags, and normalize whitespace."""
    # Strip HTML tags
    text = re.sub(r"<[^<]+?>", " ", text)
    # Strip markdown links [anchor](url) -> anchor
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Strip markdown formatting symbols (bold, italic, code, headers, bullets)
    text = re.sub(r"[*_`#~|]", " ", text)
    return text.strip()


def _clean_word(w: str) -> str:
    return re.sub(r"^[^\w]+|[^\w]+$", "", w)


def _encode_text_directive(text: str) -> str:
    """Percent-encode text directive according to W3C Scroll-to-Text-Fragment spec.

    Hyphens, ampersands, and commas carry syntactic meaning in the directive
    ('-', '&', ',') and must be explicitly percent-encoded.
    """
    encoded = urllib.parse.quote(text)
    return encoded.replace("-", "%2D").replace("&", "%26").replace(",", "%2C")


def build_universal_jump_url(base_url: str, snippet: str | None = None) -> str:
    """Build a universal W3C Scroll-to-Text-Fragment URL.

    Uses range directive `#:~:text=textStart,textEnd` to robustly highlight
    the matching excerpt across any web page (e.g. from Tavily search results).
    Targets a coherent sentence or clause to prevent cross-block DOM matching
    failures in Chromium browsers.
    """
    if not base_url:
        return ""
    clean_base = base_url.split("#")[0]
    if not snippet or not snippet.strip():
        return clean_base

    cleaned = _clean_snippet(snippet)

    # Split into candidate sentences or lines to target a single coherent DOM block
    sentences = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    candidate_words: list[str] = []
    for s in sentences:
        s_norm = re.sub(r"\s+", " ", s).strip()
        words = [_clean_word(w) for w in s_norm.split(" ") if _clean_word(w)]
        if len(words) >= 5:
            candidate_words = words
            break

    if not candidate_words:
        s_norm = re.sub(r"\s+", " ", cleaned).strip()
        candidate_words = [_clean_word(w) for w in s_norm.split(" ") if _clean_word(w)]

    if not candidate_words:
        return clean_base

    # For short phrases (<= 5 words), exact match is safest
    if len(candidate_words) <= 5:
        query = " ".join(candidate_words)
        return f"{clean_base}#:~:text={_encode_text_directive(query)}"

    # Range syntax within a single coherent sentence: take first 4 words and last 4 words
    text_start = " ".join(candidate_words[:4])
    text_end = " ".join(candidate_words[-4:])

    if not text_start:
        return clean_base
    if not text_end or text_start == text_end:
        return f"{clean_base}#:~:text={_encode_text_directive(text_start)}"

    return f"{clean_base}#:~:text={_encode_text_directive(text_start)},{_encode_text_directive(text_end)}"


def extract_best_sentence(chunk_content: str, target_assertion: str = "") -> str:
    """Extract the most relevant, self-contained sentence from chunk_content for citation snippets."""
    if not chunk_content or not chunk_content.strip():
        return ""

    cleaned = _clean_snippet(chunk_content)
    # Split by standard sentence terminators
    raw_sentences = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    candidates = [re.sub(r"\s+", " ", s).strip() for s in raw_sentences if len(s.strip()) >= 15]

    if not candidates:
        return cleaned[:160].strip()

    if not target_assertion:
        return candidates[0]

    # Find candidate with highest word overlap with target assertion
    target_words = {_clean_word(w).lower() for w in target_assertion.split() if len(_clean_word(w)) > 2}
    if not target_words:
        return candidates[0]

    best_sentence = candidates[0]
    best_score = -1

    for cand in candidates:
        cand_words = {_clean_word(w).lower() for w in cand.split() if len(_clean_word(w)) > 2}
        overlap = len(target_words & cand_words)
        if overlap > best_score:
            best_score = overlap
            best_sentence = cand

    return best_sentence


# Common function words ignored during overlap validation
STOPWORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "with", "by", "of",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "that", "this", "these", "those", "it", "they", "we", "you", "i", "he", "she",
    "là", "và", "hoặc", "trong", "ở", "tại", "đến", "cho", "với", "bởi", "của", "được",
    "có", "những", "các", "này", "đó", "khi", "để", "thì", "mà", "như", "ra", "vào",
}


def verify_citations(
    answer: str,
    chunks: Sequence[Any],
) -> tuple[str, list[int]]:
    """Verify that every citation marker [k] in answer is valid and genuinely supported by chunks.

    1. Removes out-of-range markers (k <= 0 or k > len(chunks)).
    2. Strips hallucinated markers where the cited sentence has zero substantive overlap with chunk k.

    Returns:
        (sanitized_answer, list_of_verified_indices)
    """
    if not answer or not chunks:
        return answer, []

    total_chunks = len(chunks)
    marker_pattern = re.compile(r"\[(\d+)\]")

    # Split into sentences while keeping track of markers
    sentences = re.split(r"(?<=[.!?\n])\s+", answer)
    cleaned_sentences: list[str] = []
    verified_indices: set[int] = set()

    for sentence in sentences:
        markers_in_sent = [int(m) for m in marker_pattern.findall(sentence)]
        if not markers_in_sent:
            cleaned_sentences.append(sentence)
            continue

        sent_words = {
            _clean_word(w).lower()
            for w in sentence.split()
            if len(_clean_word(w)) > 1 and _clean_word(w).lower() not in STOPWORDS
        }

        def _keep_marker(match: re.Match, sent_words: set[str] = sent_words) -> str:
            idx = int(match.group(1))
            if idx < 1 or idx > total_chunks:
                return ""  # Out-of-bounds hallucinated marker

            chunk = chunks[idx - 1]
            chunk_text = getattr(chunk, "content", str(chunk))
            chunk_words = {
                _clean_word(w).lower()
                for w in chunk_text.split()
                if len(_clean_word(w)) > 1 and _clean_word(w).lower() not in STOPWORDS
            }

            # Check overlap between assertion sentence and chunk
            overlap = sent_words & chunk_words
            if not overlap and len(sent_words) >= 3:
                # No content overlap: hallucinated citation marker
                return ""

            verified_indices.add(idx)
            return match.group(0)

        cleaned_sentence = marker_pattern.sub(_keep_marker, sentence)
        # Clean up double spaces created by marker removal
        cleaned_sentence = re.sub(r" +", " ", cleaned_sentence)
        cleaned_sentences.append(cleaned_sentence)

    sanitized_answer = " ".join(cleaned_sentences)
    return sanitized_answer, sorted(verified_indices)

