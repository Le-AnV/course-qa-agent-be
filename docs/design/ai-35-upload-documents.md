# API Design & Implementation Specs — Upload Course Materials (AI-35)

> **Ticket:** AI-35
> **Epic:** E2 - Knowledge Base
> **Status:** Planned

This document serves as the Design Specs and a hand-over reference detailing the API architecture and payload contracts for the course-material upload feature. The purpose is to provide context and guidance for the backend implementation to integrate with the frontend upload UI.

## 1. Overview

The backend needs to expose material-upload endpoints under the prefix `/api/documents` that accept up to 7 course-material formats (max 100MB per file), validate both extension AND real content, and store successful uploads with status `pending` (displayed as "pending-approval"). The AI-55 ingestion pipeline takes over afterwards.

For the initial phase, class assignment and document storage use in-memory maps (matching the E1/RBAC provisional pattern). The structure is production-ready, allowing easy replacement with SQLAlchemy tables (AI-119/AI-31) in later sprints.

## 2. API Contract

All endpoints require `Authorization: Bearer <JWT>` (AI-25/27). Trainer routes accept role `trainer` or `admin` via `require_trainer`. Student gets `403`, missing/invalid token gets `401`.

### 2.1. `GET /api/documents/accepted-types`

Lists every accepted file type for the upload screen (Upload AC-07).

**Request:**
- Headers: `Authorization: Bearer <token>`

**Response (200 OK):**
```json
[
  {"extension": ".pdf", "mime": "application/pdf", "label": "PDF document"},
  {"extension": ".docx", "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "label": "Word document"},
  {"extension": ".md", "mime": "text/markdown", "label": "Markdown"},
  {"extension": ".txt", "mime": "text/plain", "label": "Plain text"},
  {"extension": ".pptx", "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "label": "PowerPoint"},
  {"extension": ".xlsx", "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "label": "Excel"},
  {"extension": ".csv", "mime": "text/csv", "label": "CSV"}
]
```

### 2.2. `POST /api/documents/`

Multi-file material upload (Upload AC-04); each file validated independently (AC-06), per-file ok/error so one bad file never blocks the rest (AC-03).

**Request:**
- Headers: `Authorization: Bearer <token>`, `Content-Type: multipart/form-data`
- Form: `class_id` (str, must be assigned to caller) + `files: UploadFile[]`

**Response (201 Created):**
```json
{
  "uploaded": [
    {
      "file_name": "syllabus.pdf",
      "document": {
        "id": "uuid",
        "file_name": "syllabus.pdf",
        "file_type": ".pdf",
        "mime_type": "application/pdf",
        "size_bytes": 12345,
        "uploader_id": "teacher@academic.edu.vn",
        "class_id": "class-a",
        "class_name": "CS101 - Class A",
        "status": "pending",
        "created_at": "2026-10-12T00:00:00Z"
      },
      "error": null
    },
    {"file_name": "virus.exe", "document": null, "error": "Invalid file format '.exe'. Accepted: .csv, .docx, .md, .pdf, .pptx, .txt, .xlsx."}
  ]
}
```

**Error cases:**
- No/invalid token → `401/403` from `require_trainer` (student always `403`).
- Unknown `class_id` → per-file error `Class 'x' does not exist`.
- Unassigned class → per-file error `Class is not assigned to you.` (AC-05).
- Fake content (e.g. `.exe` renamed to `.pdf`) → per-file error `content is not a real PDF file`.
- Oversize → per-file error `exceeds 100MB per file` (AC-01).

## 3. Implementation Plan

### 3.1. Schemas (`app/schemas/documents.py`)
- `AcceptedTypeRead`: Pydantic model with `extension`, `mime`, `label`.
- `DocumentRead`: list/upload row with `id`, `file_name`, `file_type`, `mime_type`, `size_bytes`, `uploader_id`, `class_id`, `class_name`, `status`, `created_at`.
- `DocumentUploadResult`: one result per file with `file_name`, `document: DocumentRead | None`, `error: str | None`.
- `DocumentUploadResponse`: wrapper with `uploaded: list[DocumentUploadResult]`.

### 3.2. Services (`app/services/document_service.py`)
- `ACCEPTED_EXTENSIONS`: 7-entry map (no images; `.png/.jpg` belong to the E3 vision path).
- `MAX_FILE_SIZE_BYTES = 100MB`; `STATUS_PENDING = "pending"`.
- `detect_content_kind(extension, content)`: stdlib-only real-content check — `.pdf` starts with `%PDF`; `.docx/.pptx/.xlsx` are ZIP (`PK\x03\x04`) + `[Content_Types].xml` + `word/`/`ppt/`/`xl/` part; `.md/.txt/.csv` are UTF-8 decodable with no NUL bytes (`.csv` additionally parses via `csv`).
- `validate_upload(file_name, content, class_id, user_id)`: returns `(extension, mime, size)` or an error string (format → size → class exists → class assigned → content match).
- `create_document(...)`: validates then persists a `DocumentRecord` with `pending` status into the in-memory `_DOCUMENTS` dict (tests reset via `reset_store`).

### 3.3. Routing (`app/routers/documents/router.py`)
- Create a documents router with prefix `/api/documents`.
- Register `GET /accepted-types` and `POST /` (both `Depends(require_trainer)`).
- Mount the router in `app/main.py` via `app.include_router(documents_router)`.
- FE: `features/documents/upload-dropzone.tsx`, `lib/documents.ts` (`ACCEPTED_TYPES`, `MAX_FILE_SIZE_BYTES`).

### 3.4. Dependencies (`pyproject.toml`)
- Add `python-multipart` for `UploadFile[]` form parsing (`uv add python-multipart`).

## 4. Next Steps (Development)

1. Add `python-multipart` to dependencies.
2. Create the Pydantic schemas.
3. Implement `detect_content_kind` / `validate_upload` / `create_document` with mock class maps.
4. Expose `GET /accepted-types` and `POST /` routes behind `require_trainer`.
5. Test with curl/Postman (real PDF vs renamed `.exe`, multi-file with one bad file), then update Frontend to use the real API.

## 5. Provisional notes (replaced when E1 lands)

- Identity via Bearer JWT `sub` (AI-27); class assignment via in-memory `CLASS_ASSIGNMENTS` (no tables yet, AI-31).
- In-memory document store (DB-free, same pattern as `test_health.py`, so CI needs no postgres service).

## 6. Related integration note

- E3 `parser.py` currently routes only `.pdf/.docx/.xlsx/.xls`. `.md/.txt/.pptx/.csv` uploads are accepted here and stay `pending` until AI-49 lands — the pipeline raises `ValueError: Unsupported file format` for them meanwhile.
- Material-upload flow only. Bulk account import from `.xlsx` is a separate admin flow (T4) and never goes through this screen (AC-08).

## 7. Verify

```powershell
uv run ruff check app/services/document_service.py app/routers/documents/ tests/test_documents.py
uv run pytest tests/test_documents.py -q
```
