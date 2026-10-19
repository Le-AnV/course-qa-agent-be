# API Design & Implementation Specs — Delete & Replace Documents (AI-39)

> **Ticket:** AI-39
> **Epic:** E2 - Knowledge Base
> **Status:** Implemented

This document is the Design Specs and hand-over reference for removing and replacing course materials. The purpose: outdated materials must stop influencing chatbot answers — deleting a file removes its chunks/embeddings from Qdrant, and replacing a file (uploading with the same name) creates a new version that is re-indexed while the previous indexed version remains available until indexing succeeds.

## 1. Overview

Both operations stay inside the AI-41 class scope: a trainer only sees, deletes and replaces documents of their own classes, and another trainer's documents are never listed or reachable by id.

Key points:

- **Delete** is destructive and requires explicit confirmation (`?confirm=true`, the UI shows a confirm dialog first). Vector-backed files are purged from Qdrant **before** the row is deleted: when Qdrant is unreachable the API answers `503` and nothing is deleted, so the chatbot can never keep citing a file the trainer believes is gone (AC-02).
- **Replace** is the same upload endpoint. Uploading a file name that already exists inside the class creates the next `version` in `pending`; the trainer approves it before re-indexing. The previous version stays retrievable until the replacement indexes successfully; only then does it become `replaced` and have its chunks and stored source file purged.
- Files that were never handed to the pipeline (`pending` / `rejected` / `failed`) have no chunks, so deleting them does not require Qdrant.

## 2. API Contract

All endpoints require `Authorization: Bearer <JWT>` with role `trainer` or `admin` (`require_trainer`); students get `403`.

### 2.1. `DELETE /api/documents/{document_id}?confirm=true`

**Request:**
- Headers: `Authorization: Bearer <token>`
- Path: `document_id` (uuid)
- Query: `confirm=true` (mandatory)

**Response (200 OK):**
```json
{
  "deleted": "9f1c...",
  "file_name": "syllabus.pdf",
  "version": 1,
  "purged_chunks": 42
}
```
`purged_chunks` is `0` when the file was never indexed (or the collection does not exist yet).

**Error cases:**
- Missing/false `confirm` → `400 Deletion requires confirmation (?confirm=true).`
- Unknown id → `404 Document not found.`
- Document in a class not assigned to caller → `403 Class is not assigned to you.` (AC-04)
- Qdrant unreachable for a vector-backed (`processing` / `indexed`) file → `503 Cannot purge chunks of ...`; the row is kept (AC-02)
- Student token → `403` from `require_trainer`

### 2.2. `POST /api/documents/` — replace by file name (AC-03)

Same contract as AI-35; the per-file result now carries `version` and the replace semantics:

- **New name** → `version: 1`, `status: "pending"` (trainer approval, unchanged).
- **Existing name in the same class** (case-insensitive) → `version: prev + 1`, `status: "pending"` (trainer approval required); the previous row remains in its current status and retrievable while ingestion runs.
- After successful indexing, older versions become `replaced`, their chunks are purged, and their stored source files are removed. If ingestion fails, the prior indexed version and its vectors remain available.

```json
{
  "uploaded": [
    {
      "file_name": "syllabus.pdf",
      "document": {
        "id": "uuid",
        "file_name": "syllabus.pdf",
        "version": 2,
        "status": "processing",
        "class_id": "class-a",
        "class_name": "CS101 - Class A"
      },
      "error": null
    }
  ]
}
```

`DocumentRead` gains `version: int = 1`; `replaced` joins the statuses accepted by the list filter.

### 2.3. Qdrant purge (no HTTP; AI-39 AC-01/AC-02)

```python
delete_points_by_document_id(document_id)  # app/services/vector_store/qdrant_store.py
# points_selector = Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))])
# count first (for the response), then delete(wait=True); idempotent; 0 when the collection is missing
```
`document_service.purge_document_vectors(document_id)` wraps it and raises `VectorPurgeError` on any store failure.

### 2.4. Retrieval guard (no HTTP; consumed by E4 AC-02)

```python
document_service.retrievable_document_ids(db, class_id)      # live ids: processing | indexed
document_service.filter_retrievable_chunks(db, class_id, chunks)  # drop dead/deleted/unverifiable chunks
```
E4 RAG applies `filter_retrievable_chunks` after the Qdrant search and before answer generation, so a deleted or replaced file can never be cited even if its vectors were not fully purged yet. Chunks must carry `document_id` metadata (`build_qdrant_payload`); chunks without it are dropped.

## 3. Acceptance criteria status

| AC | Status | Where |
|---|---|---|
| **AC-01: delete requires confirmation and removes chunks from Qdrant (by file_id)** | Implemented | `?confirm=true` check + `delete_document` + `delete_points_by_document_id` (payload `document_id`) |
| **AC-02: after deletion the chatbot no longer answers from that document** | Implemented | vectors purged before the row is deleted (503 otherwise) + `filter_retrievable_chunks` retrieval guard |
| **AC-03: replacing a file (same name) creates a new version and re-indexes** | Implemented | `create_document` → `latest_version` / `version` column / `processing` handoff / prior version retired after successful indexing |
| **AC-04: trainer only deletes/replaces their own classes; others are never listed or accessible** | Implemented | `my_class_ids` scope on the delete route; list/upload scope from AI-37/41 |

## 4. Implementation Plan

### 4.1. Model + migration (`app/models/document.py`, `alembic/versions/0008_document_version.py`)
- `Document.version` (`Integer`, `NOT NULL`, default `1`); statuses add `replaced` (no enum — plain strings).
- Migration 0008 adds the column with `server_default="1"`, backfills NULLs, then drops the server default.

### 4.2. Services
- `app/services/vector_store/qdrant_store.py`: `delete_points_by_document_id(...)` — count then delete with the `document_id` filter.
- `app/services/document_service.py`:
  - `STATUS_REPLACED`, `VECTOR_BACKED_STATUSES = {processing, indexed}`, `VectorPurgeError`.
  - `latest_version(db, class_id, file_name)` — case-insensitive, highest version.
  - `purge_document_vectors(document_id)` — Qdrant wrapper, raises on failure.
  - `create_document(...)` — versioning and processing handoff; `supersede_older_versions(...)` retires prior versions only after successful indexing.
  - `delete_document(db, record)` — purge when vector-backed, then delete the row.
  - `retrievable_document_ids(...)` / `filter_retrievable_chunks(...)` — AC-02 guard.

### 4.3. Schemas + routing
- `app/schemas/documents.py`: `DocumentRead.version`, `DocumentDeleteResponse`.
- `app/routers/documents/router.py`: `DELETE /{document_id}` (400 without confirm → 404 unknown → 403 out of scope → purge → 200); upload loop reports purge failures per file.
- FE: `lib/documents.ts` (`deleteDocument`, `replaceDocument`, `replaceNameError`, `version`), `features/documents/document-list.tsx` (version column, Delete/Replace actions, `replaced` badge), `app/(shell)/teacher/materials/page.tsx` (confirm dialog + replace picker).

## 5. Next Steps / integration notes

- AI-55 wires the `processing` handoff to the real pipeline (blob storage is still discarded after validation, as in AI-35/41).
- E4 RAG must call `filter_retrievable_chunks` before generating answers.
- The FE shows version history (`replaced` rows stay listed and filterable) so trainers can audit what was superseded.

## 6. Verify

```powershell
uv run ruff check app/services/document_service.py app/services/vector_store/ app/routers/documents/ tests/test_documents.py
uv run pytest tests/test_documents.py tests/services/vector_store -q
```
