# API Design & Implementation Specs — Tag Documents by Class (AI-41)

> **Ticket:** AI-41
> **Epic:** E2 - Knowledge Base
> **Status:** Planned

This document serves as the Design Specs and a hand-over reference detailing class-scoped tagging and approval for course documents. The purpose is to keep one course's classes separated end-to-end (upload → list → RAG) and to hand approved documents to the AI-55 ingestion pipeline.

## 1. Overview

Trainers pick an existing `class_id` at upload (they never create courses/classes — admin T4 does; `class_id` is `NOT NULL`). Documents are filtered by class, approved from `pending` to `processing`, and carry a Qdrant-native payload (`document_id`, `class_id`, `file_name`, `file_type`) so E4 RAG can scope retrieval per class. Separation key is `class_id` (not `course_id`): students of class A never get citations from class B.

## 2. API Contract

Endpoints require `Authorization: Bearer <JWT>` with role `trainer` or `admin` (`require_trainer` admits admin as a superset; students get `403` before reaching the handler).

### 2.1. `GET /api/documents/classes/mine`

Single source of the caller's classes (shared with AI-35/37).

**Response (200 OK):** same `ClassMineRead[]` shape as AI-37.

### 2.2. `POST /api/documents/{document_id}/approve`

Approve `pending` → `processing` (AI-55 takes over afterwards).

**Request:**
- Headers: `Authorization: Bearer <token>`
- Path: `document_id` (uuid)

**Response (200 OK):** a `DocumentRead` with `"status": "processing"`.
```json
{
  "id": "uuid",
  "file_name": "s.pdf",
  "file_type": ".pdf",
  "mime_type": "application/pdf",
  "size_bytes": 6,
  "uploader_id": "teacher@academic.edu.vn",
  "class_id": "class-a",
  "class_name": "CS101 - Class A",
  "status": "processing",
  "created_at": "2026-10-12T00:00:00Z"
}
```

**Error cases:**
- Unknown id → `404 Document not found.`
- Document in a class not assigned to caller → `403 Class is not assigned to you.`
- Non-pending status → `400 Only pending documents can be approved (is processing).`

### 2.3. Qdrant payload (no HTTP; consumed by E4)

```python
build_qdrant_payload(doc) -> {
    "document_id": doc.id,
    "class_id": doc.class_id,
    "file_name": doc.file_name,
    "file_type": doc.file_type,
}
```

RAG filters with `{"must": [{"key": "class_id", "match": {"value": ...}}]}`; `chunk.class_id` must always equal `document.class_id`. The class filter follows the student's own class automatically (AC-05): students never pick a class by hand; sessions carry `chat_session.class_id` from enrollment (E4), staff scope comes from `class_assignments`.

## 3. Implementation Plan

### 3.1. Services (`app/services/document_service.py`)
- `VALID_STATUSES = {pending, processing, indexed, rejected, failed}`.
- `approve_document(document_id)`: `pending` → `processing`, else `KeyError`/`ValueError`.
- `build_qdrant_payload(record)`: native payload filter for class-scoped RAG (Tag AC-03/04).
- `class_name(class_id)`: display name fallback to `class_id`.
- `CLASSES` / `CLASS_ASSIGNMENTS`: in-memory stand-ins for `courses/classes/class_assignments` (AI-119/AI-31).

### 3.2. Routing (`app/routers/documents/router.py`)
- Register `POST /{document_id}/approve` (`Depends(require_trainer)`): 404 → 403 class-scope check → `approve_document` → `DocumentRead`.
- FE: class select in `features/documents/upload-dropzone.tsx`, class filter in `features/documents/document-list.tsx`.

### 3.3. Roles agreed for this task
- Trainer: upload, read, approve (delete/replace lands in AI-39).
- Teaching assistant: upload + read only, no approve (enforced by class-scope + RBAC server-side per request).
- Seeded roles stay `admin/trainer/student` until RBAC (AI-27) adds `teaching_assistant`.

## 4. Next Steps (Development)

1. Add `approve_document` / `build_qdrant_payload` / `class_name` to the service.
2. Expose `POST /{id}/approve` with 404/403/400 handling.
3. Test: student approve → `403`; other-class trainer → `403`; payload carries `class_id`.
4. Hand `processing` documents to AI-55 ingestion (E3 parser only supports `.pdf/.docx/.xlsx/.xls` for now).

## 5. Provisional notes

- `CLASS_ASSIGNMENTS` in-memory map stands in for `class_assignments(class_id, user_id, role_in_class)` (AI-119/AI-31).

## 6. Verify

```powershell
uv run ruff check app/services/document_service.py app/routers/documents/ tests/test_documents.py
uv run pytest tests/test_documents.py -q
```
