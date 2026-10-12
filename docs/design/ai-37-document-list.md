# AI-37 — Document List & Search

> Trainer story (E2 Knowledge Base). Prefix: `/api/documents`.
> Code: `app/services/document_service.py` (`list_documents`),
> `app/routers/documents/router.py` (`GET /`), schemas:
> `app/schemas/documents.py` (`DocumentRead`, `DocumentListResponse`).
> FE: `features/documents/document-list.tsx`, `lib/documents.ts`
> (`fetchDocuments`, `DocumentItem`), route `/dashboard`.
> Tests: `tests/test_documents.py` (list cases).

## Scope

- Each row shows file name, type, size, uploader, class, upload date (AC-01)
  via `DocumentRead` (`id, file_name, file_type, mime_type, size_bytes,
  uploader_id, class_id, class_name, status, created_at`).
- Search by file name, case-insensitive substring (AC-02): `?search=`.
- Filter by class, file type, processing status (AC-03):
  `?class_id= ?file_type= ?status=` (unknown status → `400`).
- Pagination when there are many files (AC-04): `?page= ?page_size=`
  (defaults `1/20`, max `100`); response carries `items/total/page/page_size`.
  Newest first.
- Only documents of the caller's assigned classes are ever returned (AC-05):
  `WHERE class_id IN my_class_ids(user_id)` — other classes are invisible,
  not just hidden in the UI.

## Endpoint

| Method | Path | Role | Notes |
|---|---|---|---|
| `GET` | `/api/documents/` | trainer, TA | `search, class_id, file_type, status, page, page_size` |

## FE behavior

- `DocumentList` holds search input + 3 filters (class/type/status) + prev/next
  pagination, all client-side over the scoped payload; `pending` renders as
  "pending approval" badge. Falls back to mock data when the backend is down.

## Provisional notes

- Same stub auth (`X-User-Id`/`X-Role`) and in-memory store as AI-35;
  swap to SQLAlchemy + E1 `GET /api/classes/mine` without changing the contract.

## Verify

```powershell
uv run ruff check app/services/document_service.py app/routers/documents/ tests/test_documents.py
uv run pytest tests/test_documents.py -q
```
