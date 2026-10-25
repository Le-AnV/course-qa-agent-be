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

1. **Hallucination Verification (`app/services/citation_service.py`):**
   - `verify_citations(answer: str, chunks: Sequence[Any]) -> tuple[str, list[int]]`:
     - Inspects all numerical markers `[k]` in the generated answer text.
     - Strips out-of-bounds indices ($k < 1$ or $k > \text{total\_chunks}$).
     - Performs word-overlap analysis between the assertion sentence containing `[k]` and chunk $k$, filtering out common stopwords in Vietnamese and English.
     - Strips hallucinated markers when there is zero substantive overlap.
     - Returns sanitized answer text and the deduplicated set of verified chunk indices.

2. **Source Formatting & Emission (`app/services/chat_service.py`):**
   - Assembles `Citation` payloads with document title, storage ID, chapter, and physical page number.
   - Emits verified citations sequentially over SSE as `citation` events immediately following token streaming.
