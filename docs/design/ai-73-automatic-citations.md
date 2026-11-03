# AI-73 — Automatic Citations in Answers

> **Ticket:** AI-73 (Epic E5: Citations, Task T21) · **Status:** Implemented
> **Branch:** `features/Implementation_Citations`
> **Related services:** `app/services/citation_service.py`, `app/services/chat_service.py`.

## Purpose

Ensures every assistant answer grounded in course materials contains transparent citation markers (`[1]`, `[2]`) pointing directly to the used chunks, formatted with file name, class, chapter, and page/slide information, while rigorously eliminating hallucinated or out-of-bounds citations.

## Key Acceptance Criteria

- **AC-01:** Every answer includes source metadata corresponding to the exact chunks used for generation.
- **AC-02:** Citations are clearly formatted: file name, class, chapter, and page/slide number.
- **AC-03:** Only chunks that actually contributed to the answer are cited (unreferenced chunks are not displayed).
- **AC-04:** If no source is found, zero citations are fabricated.

## Backend Implementation

1. **Hallucination Verification & Passage Selection (`app/services/citation_service.py`):**
   - `verify_citations(answer: str, chunks: Sequence[Any]) -> tuple[str, list[int]]`:
     - Inspects all numerical markers `[k]`, `[k, m]` in the generated answer text (preserving code fences and inline blocks).
     - Strips out-of-bounds indices ($k < 1$ or $k > \text{total\_chunks}$).
     - Performs lexical word-overlap guard between assertion sentences and chunk content.
     - Strips hallucinated markers when there is no supporting content overlap.
     - Returns sanitized answer text and verified citation indices.
   - `extract_cited_passages(answer: str, chunks: Sequence[Any], *, question: str = "") -> dict[int, tuple[str, ...]]`:
     - Resolves cited verbatim sentences, list items, table rows, or full overview sections to populate citation highlights.

2. **Source Formatting & Emission (`app/services/rag_service.py`, `app/services/chat_service.py`):**
   - `cited_page_citations(chunks, answer, max_citations=3, *, question="") -> list[Source]`:
     - Collapses same-page chunks into single citations carrying deduplicated `indexes: tuple[int, ...]` and exact `highlights: tuple[str, ...]`.
     - Preserves breadcrumb-enriched context formatting via `_curriculum_context_line(index, chunk)`.
   - Emits verified citations sequentially over SSE as `citation` events immediately following token streaming, matching the `ChatCitation` schema in `app/schemas/history.py`.
