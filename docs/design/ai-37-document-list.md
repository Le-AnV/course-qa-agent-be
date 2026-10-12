# API Design & Implementation Specs — Document List & Search (AI-37)

> **Ticket:** AI-37
> **Epic:** E2 - Knowledge Base
> **Status:** Planned

This document serves as the Design Specs and a hand-over reference detailing the API architecture and payload contracts for browsing/searching course documents. The purpose is to provide context and guidance for the backend implementation to integrate with the frontend document-list UI.

## 1. Overview

The backend needs to expose `GET /api/documents/classes/mine` (the caller's assigned classes, Upload AC-05 / List AC-05) and `GET /api/documents/` (browse/search/filter/paginate, List AC-01..05). Only documents of the caller's assigned classes are ever returned — other classes are invisible server-side, not just hidden in the UI.

## 2. API Contract

Both endpoints require `Authorization: Bearer <JWT>` with role `trainer` or `admin` (`require_trainer`). Student gets `403`, missing/invalid token gets `401`.

### 2.1. `GET /api/documents/classes/mine`

**Request:**
- Headers: `Authorization: Bearer <token>`

**Response (200 OK):**
```json
[
  {"id": "class-a", "course_id": "course-cs101", "name": "CS101 - Class A", "code": "CS101-A"},
  {"id": "class-b", "course_id": "course-cs101", "name": "CS101 - Class B", "code": "CS101-B"}
]
```

### 2.2. `GET /api/documents/`

**Request:**
- Headers: `Authorization: Bearer <token>`
- Query: `search` (case-insensitive filename substring, default `""`), `class_id`, `file_type`, `status`, `page` (default `1`), `page_size` (default `20`, max `100`)

**Response (200 OK):**
```json
{
  "items": [
    {
      "id": "uuid",
      "file_name": "algebra.pdf",
      "file_type": ".pdf",
      "mime_type": "application/pdf",
      "size_bytes": 12345,
      "uploader_id": "teacher@academic.edu.vn",
      "class_id": "class-a",
      "class_name": "CS101 - Class A",
      "status": "pending",
      "created_at": "2026-10-12T00:00:00Z"
    }
  ],
  "total": 3,
  "page": 1,
  "page_size": 20
}
```

**Notes:** newest first; unknown `status` → `400 Unknown status 'x'.`

## 3. Implementation Plan

### 3.1. Schemas (`app/schemas/documents.py`)
- `ClassMineRead`: Pydantic model with `id`, `course_id`, `name`, `code` (source: E1 when it lands).
- Reuse `DocumentRead` from AI-35 for each row (file name, type, size, uploader, class, upload date, AC-01).
- `DocumentListResponse`: wrapper with `items`, `total`, `page`, `page_size`.

### 3.2. Services (`app/services/document_service.py`)
- `my_class_ids(user_id)`: classes assigned to the caller.
- `my_classes(user_id)`: full class dicts for `classes/mine`.
- `list_documents(user_id, search, class_id, file_type, status, page, page_size)`: scope to `my_class_ids` first, then substring search, then optional filters, sort newest-first, then paginate.

### 3.3. Routing (`app/routers/documents/router.py`)
- Register `GET /classes/mine` and `GET /` on the documents router (both `Depends(require_trainer)`).
- FE: `features/documents/document-list.tsx`, `lib/documents.ts` (`fetchDocuments`, `DocumentItem`), route `/dashboard`. `DocumentList` holds search input + 3 filters (class/type/status) + prev/next pagination; `pending` renders as "pending approval" badge, with mock fallback when the backend is down.

## 4. Next Steps (Development)

1. Add `my_classes` / `list_documents` to the document service.
2. Add `ClassMineRead` / `DocumentListResponse` schemas.
3. Expose `GET /classes/mine` and `GET /` behind `require_trainer`.
4. Test search/filter/pagination scoping (single-class user only sees their class; student gets `403`).

## 5. Provisional notes

- Same Bearer-JWT auth and in-memory store as AI-35; swap to SQLAlchemy + E1 `GET /api/classes/mine` without changing the contract.

## 6. Verify

```powershell
uv run ruff check app/services/document_service.py app/routers/documents/ tests/test_documents.py
uv run pytest tests/test_documents.py -q
```
