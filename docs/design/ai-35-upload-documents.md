# AI-35 — Upload Course Materials

> Trainer story (E2 Knowledge Base). Prefix: `/api/documents`.
> Code: `app/services/document_service.py` (`validate_upload`, `detect_content_kind`,
> `create_document`), `app/routers/documents/router.py` (`POST /`,
> `GET /accepted-types`), schemas: `app/schemas/documents.py`
> (`DocumentUploadResponse`, `AcceptedTypeRead`).
> FE: `features/documents/upload-dropzone.tsx`, `lib/documents.ts`
> (`ACCEPTED_TYPES`, `MAX_FILE_SIZE_BYTES`).
> Tests: `tests/test_documents.py` (upload cases).

## Scope

- Accepts 7 formats, max 100MB per file (AC-01): `.pdf .docx .md .txt .pptx
  .xlsx .csv`. Image formats (`.png/.jpg`) belong to the E3 vision path, not here.
- Successful upload is stored with status `pending`, displayed as
  "pending-approval" (AC-02). The AI-55 ingestion pipeline takes over afterwards.
- Invalid formats rejected with a clear per-file message; other files in the
  same request still succeed (AC-03).
- Multi-file upload in one request (AC-04): multipart `files: UploadFile[]`.
- Uploader picks `class_id` from their own assigned classes only; unknown class
  → `404`, unassigned class → per-file `403`-style error (AC-05).
- Validation checks extension AND real content (AC-06, stdlib only):

  | Ext | Real-content rule |
  |---|---|
  | `.pdf` | starts with `%PDF` |
  | `.docx/.pptx/.xlsx` | ZIP (`PK\x03\x04`) + `[Content_Types].xml` + `word/` / `ppt/` / `xl/` part |
  | `.md/.txt/.csv` | UTF-8 decodable, no NUL bytes (`.csv` additionally parses via `csv`) |

- `GET /api/documents/accepted-types` lists every accepted type for the upload
  screen (AC-07).
- Material-upload flow only. Bulk account import from `.xlsx` is a separate
  admin flow (T4) and never goes through this screen (AC-08).

## Endpoints

| Method | Path | Role | Notes |
|---|---|---|---|
| `GET` | `/api/documents/accepted-types` | trainer, TA | 7 accepted types (AC-07) |
| `POST` | `/api/documents/` | trainer, TA | form `class_id` + files; `201` with per-file ok/error |

## Provisional notes (replaced when E1 lands)
- Identity via stub headers `X-User-Id` / `X-Role` (no JWT yet, AI-27);
  class assignment via in-memory `CLASS_ASSIGNMENTS` (no tables yet, AI-31).
- In-memory document store (DB-free, same pattern as `test_health.py`,
  so CI needs no postgres service).

## Related integration note

- E3 `parser.py:13-18` currently routes only `.pdf/.docx/.xlsx/.xls`.
  `.md/.txt/.pptx/.csv` uploads are accepted here and stay `pending` until
  AI-49 (Extract Text from Word/MD/TXT/PPTX/XLSX/CSV, To Do) lands — the
  pipeline will raise `ValueError: Unsupported file format` for them meanwhile.

## Verify

```powershell
uv run ruff check app/services/document_service.py app/routers/documents/ tests/test_documents.py
uv run pytest tests/test_documents.py -q
```
