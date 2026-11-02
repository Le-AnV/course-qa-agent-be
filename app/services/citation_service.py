"""Citations helper service for deep-linking and text fragment generation."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Iterator, Sequence
from typing import Any

from app.services.ingestion.chunker import _fenced_ranges
from app.services.ingestion.cleaner import _PROTECTED, _protect, _restore, _strip_emphasis

_MARKER_PATTERN = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_LIST_ITEM_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:[-+*]|\d+[.)])[ \t]+[^\n]*"
    r"(?:\n(?![ \t]*(?:$|#{1,6}[ \t]|[-+*][ \t]|\d+[.)][ \t]))[^\n]+"
    r"|\n[ \t]*\n[ \t]{2,}[^\n]+)*"
)


def _clean_snippet(text: str) -> str:
    """Strip markdown formatting, HTML tags, and normalize whitespace."""
    # Strip HTML tags
    text = re.sub(r"<[^<]+?>", " ", text)
    # Strip markdown links [anchor](url) -> anchor
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Strip markdown formatting symbols (bold, italic, code, headers, bullets)
    text = re.sub(r"[*_`#~|]", " ", text)
    return text.strip()


def normalize_search_url(url: str | None) -> str:
    """Make provider-relative redirect URLs safe to store and open."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("//"):
        cleaned = f"https:{cleaned}"
    if cleaned.startswith(("/goto?url=", "goto?url=")):
        return f"https://www.google.com/{cleaned.lstrip('/')}"
    return cleaned


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
    clean_base = normalize_search_url(base_url).split("#")[0]
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


def extract_best_sentence(
    chunk_content: str, target_assertion: str = "", *, question: str = "",
) -> str:
    """Select a source sentence, complete list item, row, or code block verbatim."""
    fragments: list[str] = []
    cursor = 0
    ranges = _fenced_ranges(chunk_content)
    ranges = sorted([*ranges, *(
        match.span() for match in _LIST_ITEM_PATTERN.finditer(chunk_content)
        if not any(start <= match.start() < end for start, end in ranges)
    )])
    for start, end in ranges:
        fragments.extend(re.split(r"(?<=[.!?])\s+|\n+", chunk_content[cursor:start]))
        fragments.append(chunk_content[start:end])
        cursor = end
    fragments.extend(re.split(r"(?<=[.!?])\s+|\n+", chunk_content[cursor:]))
    candidates = list(dict.fromkeys(
        sentence.strip()
        for sentence in fragments
        if sentence.strip()
        and not re.match(r"^\s*#{1,6}\s", sentence)
        and not re.fullmatch(r"[\s|:\-]+", sentence)
    ))
    if not candidates:
        return ""
    if not target_assertion:
        return candidates[0]
    if target_assertion.startswith(("```", "~~~")):
        body = " ".join("\n".join(target_assertion.splitlines()[1:-1]).split())
        exact = [
            candidate for candidate in candidates
            if candidate.startswith(("```", "~~~"))
            and " ".join("\n".join(candidate.splitlines()[1:-1]).split()) == body
        ]
        if exact:
            return exact[0] if len(exact) == 1 else ""
    target_words = _content_words(target_assertion)
    question_words = _content_words(question)
    literals = re.findall(r"`([^`\n]+)`", target_assertion)
    literal_pattern = ".*?".join(re.escape(literal) for literal in literals)
    negations = {"not", "never", "no", "không", "chẳng", "chưa"}
    scores = [
        (
            len(target_words & words)
            if candidate.startswith(("```", "~~~"))
            or bool(target_words & negations) == bool(words & negations)
            else 0,
            bool(literals) and bool(re.search(literal_pattern, candidate, re.DOTALL)),
            len(question_words & words),
        )
        for candidate in candidates
        for words in [_content_words(candidate)]
    ]
    best = max(scores)
    if best[0] == 0 or scores.count(best) != 1:
        return ""
    return candidates[scores.index(best)]


# Common function words ignored during overlap validation
STOPWORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "with", "by", "of",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "that", "this", "these", "those", "it", "they", "we", "you", "i", "he", "she",
    "là", "và", "hoặc", "trong", "ở", "tại", "đến", "cho", "với", "bởi", "của", "được",
    "có", "những", "các", "này", "đó", "khi", "để", "thì", "mà", "như", "ra", "vào",
}


def _content_words(text: str) -> set[str]:
    """Keep technical identifiers and comment/trigraph operators in lexical matching."""
    text, blocks = _protect(text)
    text = _restore(_strip_emphasis(text), blocks)
    return {
        word for word in re.findall(
            r"\w+(?:[-.]\w+)*|/\*|\*/|//|\?\?[=/'()!<>-]", text.casefold(),
        )
        if len(word) > 1 and word not in STOPWORDS
    }


def _section_terms(text: str) -> set[str]:
    """Normalize regular plurals only for section-intent matching."""
    return {
        word.removesuffix("s") if len(word) > 3 and not word.endswith("ss") else word
        for word in _content_words(text)
    }


def _cited_assertions(answer: str) -> Iterator[tuple[re.Match[str], str]]:
    """Associate markers with preceding paragraphs or code, preserving soft wraps."""
    # Some generated answers put citations directly after a closing fence.
    unmarked = _MARKER_PATTERN.sub(lambda match: " " * len(match[0]), answer)
    fences = _fenced_ranges(unmarked)
    for position, (start, end) in enumerate(fences):
        line_start = answer.rfind("\n", start, end - 1) + 1
        trailing = _MARKER_PATTERN.search(answer, line_start, end)
        if trailing and re.fullmatch(r"[ \t]*(?:`{3,}|~{3,})[ \t]*", answer[line_start:trailing.start()]):
            fences[position] = (start, trailing.start())
    unfenced = list(answer)
    for start, end in fences:
        unfenced[start:end] = ["\n" if char == "\n" else " " for char in answer[start:end]]
    protected = [*fences, *(match.span() for match in _PROTECTED.finditer("".join(unfenced)))]
    previous_end = 0
    previous_assertion = ""
    for match in _MARKER_PATTERN.finditer(answer):
        if any(start <= match.start() < end for start, end in protected):
            continue
        prefix = answer[previous_end:match.start()].strip(" \t\r\n.,;:")
        assertion = re.split(
            r"\n[ \t]*\n|\n(?=[ \t]*(?:#{1,6}[ \t]|[-+*][ \t]|\d+[.)][ \t]))",
            prefix,
        )[-1] if prefix else previous_assertion
        assertion = re.sub(r"(?m)^#{1,6}[ \t]+[^\n]*(?:\n|$)", "", assertion).strip()
        for start, end in reversed(fences):
            if end <= match.start() and not _MARKER_PATTERN.sub("", answer[end:match.start()]).strip():
                assertion = answer[start:end].strip()
                break
        yield match, assertion
        previous_end = match.end()
        previous_assertion = assertion


def cited_markers(answer: str) -> set[int]:
    """Read explicit source markers while ignoring indexes inside code or math."""
    return {
        int(index) for match, _assertion in _cited_assertions(answer)
        for index in match.group(1).split(",")
    }


def _verified_citations(
    answer: str, chunks: Sequence[Any],
) -> Iterator[tuple[re.Match[str], list[int], str]]:
    """Share range/lexical checks between marker validation and passage selection."""
    for match, assertion in _cited_assertions(answer):
        indexes = list(dict.fromkeys(int(part) for part in match.group(1).split(",")))
        line_start = answer.rfind("\n", 0, match.start()) + 1
        if not answer[line_start:match.start()].strip():
            remainder = answer[match.end():].split("\n", 1)[0].strip()
            for index in indexes:
                title = getattr(chunks[index - 1], "title", None) if 1 <= index <= len(chunks) else None
                if title and remainder.startswith(f"{title}: "):
                    assertion = remainder[len(title) + 2:]
                    break
        words = _content_words(assertion)
        valid = []
        for index in indexes:
            if not 1 <= index <= len(chunks):
                continue
            content = getattr(chunks[index - 1], "content", str(chunks[index - 1]))
            if len(words) < 3 or words & _content_words(content):
                valid.append(index)
        yield match, valid, assertion


def verify_citations(answer: str, chunks: Sequence[Any]) -> tuple[str, list[int]]:
    """Remove invalid source markers using range checks and a lexical guard.

    This guard does not prove semantic entailment or validate generated facts.
    Code and math formatting are preserved.
    """
    verified = {match.start(): valid for match, valid, _ in _verified_citations(answer, chunks)}

    def replace(match: re.Match[str]) -> str:
        valid = verified.get(match.start())
        if valid is None:
            return match.group(0)
        return f"[{', '.join(map(str, valid))}]" if valid else ""

    return _MARKER_PATTERN.sub(replace, answer), sorted({i for ids in verified.values() for i in ids})


def extract_cited_passages(
    answer: str, chunks: Sequence[Any], *, question: str = "",
) -> dict[int, tuple[str, ...]]:
    """Keep complete items or a retrieved same-page section under its original markers."""
    passages: dict[int, list[str]] = {}
    overview_words = {
        "what", "which", "list", "all", "summarize", "summarise", "overview",
        "give", "provide", "explain", "describe", "about", "please",
    }
    for _match, valid, assertion in _verified_citations(answer, chunks):
        for index in valid:
            selected = passages.setdefault(index, [])
            chunk = chunks[index - 1]
            section = chunk.metadata.get("section_path", "")
            heading_words = _section_terms(section.rsplit(" > ", 1)[-1])
            labels = [
                _section_terms(label)
                for member in chunks
                if member.metadata.get("section_path") == section
                for label in re.findall(r"(?m)^[ \t]*[-+*][ \t]+\*\*([^*]+)\*\*", member.content)
            ]
            overview = any(
                bool(heading_words) and heading_words <= question_words
                and question_words - heading_words <= overview_words
                and not any(label and label <= question_words for label in labels)
                for line in question.splitlines()
                if (question_words := _section_terms(line))
            )
            members = [chunk]
            if overview:
                members = sorted(
                    (
                        member for member in chunks
                        if member.document_id == chunk.document_id and member.page == chunk.page
                        and member.metadata.get("section_path") == section
                    ),
                    key=lambda member: member.metadata.get("chunk_index", 0),
                )
            for member in members:
                if overview:
                    candidates = [member.content]
                else:
                    protected, blocks = _protect(assertion)
                    targets = [assertion] if assertion.startswith(("```", "~~~")) else [
                        _restore(part, blocks)
                        for part in re.split(r"(?<=[.!?])\s+|\n+", protected)
                        if part.strip()
                    ]
                    candidates = [
                        extract_best_sentence(member.content, target, question=question)
                        for target in targets
                    ]
                for passage in candidates:
                    if passage and passage not in selected:
                        selected.append(passage)
    return {index: tuple(selected) for index, selected in passages.items()}

