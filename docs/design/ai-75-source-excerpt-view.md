# AI-75 — Click a Citation to View the Source Excerpt (Backend)

> **Ticket:** AI-75 (Epic E5: Citations, Task T22) · **Status:** Implemented
> **Branch:** `features/Implementation_Citations`
> **Related modules:** `app/services/pdf_highlight_service.py`, `app/routers/chat/router.py`, `app/services/citation_service.py`.

## Purpose

Provides backend APIs and calculation engines that allow students to click any citation chip to inspect the exact original text excerpt (chunk) and retrieve visual bounding-box coordinates for PDF highlighting, without transferring the heavy full PDF binary.

## Key Acceptance Criteria

- **AC-01:** Clicking a citation opens a panel/tooltip showing the original text excerpt (chunk).
- **AC-02:** The excerpt highlights the related keywords.
- **AC-03:** Provides a direct link to open the original document.

## Backend Implementation

1. **Passage and Best Sentence Extraction (`app/services/citation_service.py`):**
   - `extract_best_sentence(chunk_content: str, target_assertion: str, *, question: str = "") -> str`:
     - Selects a complete source sentence, full list item (`_LIST_ITEM_PATTERN`), table row, or code block verbatim.
     - Preserves code/math fence blocks and aligns with question keywords for excerpt previews.
   - `extract_cited_passages(answer: str, chunks: Sequence[Any], *, question: str = "") -> dict[int, tuple[str, ...]]`:
     - Extracts the exact passages corresponding to verified citation markers `[k]`, supporting overview queries by assembling same-page section chunks.

2. **W3C Scroll-to-Text Directive (`app/services/citation_service.py`):**
   - `normalize_search_url(url: str | None) -> str`:
     - Normalizes SerpApi relative redirect URLs (`/goto?url=`) into absolute Google URLs.
   - `build_universal_jump_url(base_url: str, snippet: str | None = None) -> str`:
     - Generates compliant W3C Scroll-to-Text-Fragment fragments (`#:~:text=textStart,textEnd`).
     - Preserves clean URLs without trailing hashes for Google redirect links to avoid dropping fragments on 302 hops.

3. **PDF Bounding Box Coordinate Service (`app/services/pdf_highlight_service.py`):**
   - `get_highlight_rects(pdf_path: str | Path, target_page: int, passages: Sequence[str]) -> list[dict]`:
     - Inspects target PDF page using PyMuPDF (`fitz`).
     - Matches full continuous token sequences (`_TOKEN_PATTERN = r"\w+(?:\.\w+)*|[^\w\s]"`) with soft hyphenation (`\u00ad`) and line-break dehyphenation support.
     - Enforces single exact match semantics: bounding rects are emitted only when a cited passage matches at exactly one unique location on the target page.
     - Returns normalized bounding boxes: `[{ "page": int, "x": float, "y": float, "width": float, "height": float }]`.
   - `highlight_pdf(pdf_path, target_page, passages) -> bytes`:
     - Produces an in-memory highlighted PDF stream for native previewing.

4. **REST Highlighting Endpoint (`app/routers/chat/router.py`):**
   - `POST /api/chat/sources/{document_id}/highlight`:
     - Authenticates user and verifies class access.
     - Accepts `{ "page": int, "passages": list[str] }`.
     - Returns highlighted PDF binary stream with exact yellow rect highlights.
