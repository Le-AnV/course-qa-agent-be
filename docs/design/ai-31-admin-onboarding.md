# API Design — Admin: Courses, Classes, Accounts, Import & Email (AI-31, AI-105)

> Design deliverable for the Admin onboarding story. Related: AI-31 (Khang Vu),
> AI-25/AI-27 (auth/RBAC), AI-105 (email sender).
> Base URL (local): `http://localhost:8000`
>
> Auth: every endpoint below (except `/health`) requires
> `Authorization: Bearer <JWT>` with `role == "admin"`.
> Non-admin → `403`; missing/invalid token → `401`.

## Conventions

- Errors use FastAPI's default `{"detail": "..."}` shape.
- Login errors are always the generic `"Invalid email or password."` (AI-25) —
  they never reveal whether an email exists, whether the account is inactive,
  or whether a trainer/TA is still awaiting class assignment (AC-03).
- The admin **never sees generated passwords** (AC-07): create/import/reset
  responses carry no password; the plaintext exists only in the queued email body.

## Auth

### POST /api/auth/login

```json
{ "email": "admin@example.com", "password": "Admin123!" }
```

→ `200 { access_token, token_type: "bearer", role, user_id, username, class_id }`
→ `401 { detail: "Invalid email or password." }`

### GET /api/auth/me

→ `200 { id, username, email, role, class_id, is_active }`

## Courses (AC-01)

### POST /api/admin/courses — `201 { id, code, title, class_count }`

```json
{ "code": "SE101", "title": "Intro to Software Engineering" }
```

Duplicate code → `400`.

### GET /api/admin/courses — `200 [{ id, code, title, class_count }]`

## Classes (AC-01, AC-02, AC-12)

### POST /api/admin/classes — `201 ClassRead`

```json
{ "course_id": 1, "code": "SE101-A" }
```

Unknown course → `400` (a class cannot be assigned before it exists).
Duplicate code within the course → `400`.

### GET /api/admin/classes?course_id=1 — `200 [ClassRead]`

`ClassRead`:

```json
{
  "id": 1, "course_id": 1, "course_code": "SE101", "code": "SE101-A",
  "trainer_id": 3, "trainer_name": "thayhung",
  "ta_id": null, "ta_name": null,
  "student_count": 42, "document_count": 5, "ticket_count": 1
}
```

### PATCH /api/admin/classes/{id}/assign — `200 ClassRead`

```json
{ "trainer_id": 3, "ta_id": null }
```

Exactly one trainer per class; the same trainer may teach several classes.
Non-trainer id as `trainer_id` → `400`.

### DELETE /api/admin/classes/{id} — `200`

```json
{ "deleted": 1, "document_count": 5, "ticket_count": 1, "member_count": 43 }
```

Members are unlinked (accounts kept). The dashboard warns first when
`document_count + ticket_count > 0` (AC-12).

## Accounts (AC-03, AC-11, AC-12)

### POST /api/admin/users — `201 UserRead`

```json
{ "username": "Nguyen Van A", "email": "a@gmail.com", "role": "student", "class_code": "SE101-A" }
```

- `role`: `student | trainer | teaching_assistant` (admin never via API).
- Trainer/TA **require** `class_code` (AC-03); unknown class → `400`.
- Existing email → `400`. Password is generated, queued by email, never returned.

### GET /api/admin/users?role=&class_id= — `200 [UserRead]`

### PATCH /api/admin/users/{id}/active — `200 UserRead`

```json
{ "is_active": false }
```

Deactivated accounts fail login with the generic error (AC-11).

### POST /api/admin/users/{id}/reset-password — `200`

```json
{ "user_id": 7, "emailed_to": "a@gmail.com" }
```

New random password, emailed; response carries no password (AC-11).

### DELETE /api/admin/users/{id}?confirm=true — `200 { deleted }`

Missing `?confirm=true` → `400`. Last admin → `400` (AC-12).

## Bulk import (AC-05..AC-10)

### GET /api/admin/import-template

Downloads `account-import-template.xlsx`: sheet `accounts` with header exactly
`fullname, email, type` + one example row, plus a `Hướng dẫn` guide sheet.
No password or code columns (AC-05, AC-06). MSSV/MSNV are generated from the
full name as `INITIALS_NNNN` (Tran Van A -> `TVA_4821`), where the 4 digits are
random and unique within the same initials prefix. Students receive a MSSV;
trainers/TAs receive an MSNV. Legacy 4/5-column files with `mssv`/`msnv` are
still accepted; explicit codes must match `^[A-Z]+_[0-9]{4}$` and be unique.

### POST /api/admin/imports — `201 ImportBatchRead` (multipart `file: *.xlsx`)

```json
{ "id": 1, "filename": "class.xlsx", "total_rows": 40,
  "imported_count": 38, "skipped_count": 1, "failed_count": 1, "created_at": "..." }
```

- Bad header / empty file / non-xlsx → `400` with the expected header spelled out.
- Per row: imported | skipped (email exists) | failed (role/class invalid).
- Each imported account: random password → **queued** email (AC-07). The
  admin drains the outbox via `POST /api/admin/email-queue/process` at the
  limited rate (AC-09) — nothing is sent inline during import, so a whole
  class never hits the Gmail limit at once.

### GET /api/admin/imports — `200 [ImportBatchRead]`

### GET /api/admin/imports/{id} — `200 ImportBatchDetail` (batch + `rows[]`)

`ImportRowRead`: `{ id, row_number, username, email, role_raw, class_raw, status, message, student_code }`

### POST /api/admin/import-rows/{rowId}/retry — `200 ImportRowRead`

Only `failed` rows; re-validates class/role, then imports without touching
the rest of the file (AC-10).

## Email queue (AC-08, AC-09, AI-105)

### GET /api/admin/email-config — `200`

```json
{ "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_sender": "noreply@school.edu",
  "has_app_password": true, "smtp_rate_per_minute": 20, "email_dry_run": true }
```

App password is **masked** — never returned, never logged.

### PATCH /api/admin/email-config — `200` (same shape)

Write-only `smtp_app_password` (blank = keep). DB-backed config overrides env.

### POST /api/admin/email-config/test — `200 { sent_to, processed, sent, failed }`

Body: `{ "to_email": "you@gmail.com" }`. Misconfigured sender → `502`.

### GET /api/admin/email-queue/stats — `200 { pending, sent, failed }`

### GET /api/admin/email-queue?status=failed — `200 [EmailOutboxRead]`

Bodies are **not** listed (passwords live only in queued bodies, never in a GET).

### POST /api/admin/email-queue/process — `200 { processed, sent, failed }`

Drains pending oldest-first with `60 / rate_per_minute` delay between sends.
In dry-run mode everything is marked sent without real SMTP.

### POST /api/admin/email-queue/{id}/retry — `200 { id, status }`

Re-queues a failed email only.

## Roles

### GET /api/admin/roles — `200 { roles: ["student", "trainer", "teaching_assistant"] }`

## Class scoping (AC-04)

Non-admin callers carry `class_id` (login response + `/me`). Chat/RAG
services **must** filter documents and history by the caller's own
`class_id` — enforced server-side via `require_class_member`, never in the UI.
