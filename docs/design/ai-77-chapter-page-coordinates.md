# AI-77 — Citations Point to the Correct Chapter/Page (Backend)

> **Ticket:** AI-77 (Epic E5: Citations, Task T23) · **Status:** Implemented
> **Branch:** `features/Implementation_Citations`
> **Related modules:** `app/services/document_service.py`, `app/services/chat_context_service.py`.

## Purpose

Guarantees that page numbers and chapter metadata captured during document ingestion and chunking (E3) are strictly preserved across vector embedding, retrieval, reranking, and citation generation, ensuring cited coordinates precisely match physical pages in source PDF and lecture slide files.

## Key Acceptance Criteria

- **AC-01:** Page and chapter metadata saved at chunking time is never lost after vector embedding.
- **AC-02:** For PDFs, the page in the citation matches the physical page of the file.
- **AC-03:** Tested and verified across multiple file formats (PDF, DOCX, etc.).

## Backend Implementation

1. **Metadata Retention Across Retrieval Pipeline:**
   - Vector chunk payloads in Qdrant store `page`, `chapter`, and `slide` attributes.
   - Retrieval and Cross-Encoder reranking pipelines retain payload fields in `ScoredChunk` instances.
   - SSE citation emitters format metadata into `page: int` and `title: str`.

2. **Cross-Environment Document Path Resolution (`app/services/document_service.py`):**
   - Added robust filename matching fallback under `settings.document_storage_dir`:
     ```python
     direct_match = (root / stored.name).resolve()
     if direct_match.is_relative_to(root) and direct_match.is_file():
         return direct_match
     ```
   - Prevents file resolution failures when documents were ingested across different environments or host mount paths.
