# AI-41 — Tag Documents by Class

> Trainer story (E2 Knowledge Base). Prefix: `/api/documents`.
> Code: `app/services/document_service.py` (`my_class_ids`, `my_classes`,
> `build_qdrant_payload`, `approve_document`),
> `app/routers/documents/router.py` (`GET /classes/mine`,
> `POST /{id}/approve`).
> FE: class select in `features/documents/upload-dropzone.tsx`, class filter in
> `features/documents/document-list.tsx`.
> Tests: `tests/test_documents.py` (scope + approve + payload cases).

## Scope

- Choose a class when uploading; the class must already exist — trainers never
  create courses/classes, admin (T4) sets those up (AC-01). `class_id` is
  `NOT NULL` on every document; unknown class → error.
- Filter documents by class (AC-02): `GET /api/documents/?class_id=` plus the
  FE class filter; `GET /api/documents/classes/mine` is the single source of
  the caller's classes.
- A question can be scoped to the chosen class via Qdrant native payload filter
  (AC-03): `build_qdrant_payload(doc) →
  {"document_id", "class_id", "file_name", "file_type"}`; RAG filters with
  `{"must": [{"key": "class_id", "match": {"value": ...}}]}` (E4 consumes this).
- One course with several classes keeps documents separate (AC-04): separation
  key is `class_id` (not `course_id`); `chunk.class_id` must always equal
  `document.class_id`. Students of class A never get citations from class B.
- The class filter follows the student's own class automatically (AC-05):
  students never pick a class by hand. Sessions carry `chat_session.class_id`
  derived from their enrollment (E4); staff scope comes from
  `class_assignments` (1 trainer teaches many classes; 1 class may have
  primary + assistants).

## Endpoints

| Method | Path | Role | Notes |
|---|---|---|---|
| `GET` | `/api/documents/classes/mine` | trainer, TA | Classes assigned to caller |
| `POST` | `/api/documents/{id}/approve` | trainer only | `pending → processing`; TA gets `403` |

## Roles agreed for this task

- Trainer: upload, read, approve, (delete/replace lands in AI-39).
- Teaching assistant: upload + read only, no approve.
- Seeded roles stay `admin/trainer/student` until RBAC (AI-27) adds
  `teaching_assistant`; enforcement is server-side per request.

## Provisional notes

- `CLASS_ASSIGNMENTS` in-memory map stands in for the future
  `class_assignments(class_id, user_id, role_in_class)` table (AI-119/AI-31).

## Verify

```powershell
uv run ruff check app/services/document_service.py app/routers/documents/ tests/test_documents.py
uv run pytest tests/test_documents.py -q
```
