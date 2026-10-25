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

1. **Best Sentence Extraction (`app/services/citation_service.py`):**
   - `extract_best_sentence(chunk_content: str, target_assertion: str) -> str`:
     - Parses sentences from the chunk text.
     - Computes lexical overlap with the cited assertion sentence.
     - Returns the most representative sentence for excerpt previews and tooltips.

2. **W3C Scroll-to-Text Directive (`app/services/citation_service.py`):**
   - `build_universal_jump_url(file_url, target_quote, prefix, suffix)`:
     - Formats compliant W3C Scroll-to-Text-Fragment fragments (`#:~:text=[prefix-,]start,end[,-suffix]`).
     - Enables browsers to jump directly to and highlight text natively on HTML / Web pages.

3. **PDF Bounding Box Coordinate Service (`app/services/pdf_highlight_service.py`):**
   - `get_highlight_rects(pdf_path, search_phrase, target_page) -> list[dict]`:
     - Inspects target PDF pages using PyMuPDF (`fitz`).
     - Performs multi-tier fuzzy and normalized phrase matching.
     - Filters out short, isolated random matches (requiring substantive phrases of $\ge 3$ words).
     - Returns normalized bounding boxes: `[{ "page": int, "x": float, "y": float, "width": float, "height": float }]`.

4. **REST Highlighting Endpoint (`app/routers/chat/router.py`):**
   - `POST /api/chat/sources/{document_id}/rects`:
     - Authenticates user and verifies class access.
     - Accepts `{ "search_phrase": str, "target_page": int }`.
     - Returns calculated bounding boxes, enabling high-performance frontend overlay rendering.
