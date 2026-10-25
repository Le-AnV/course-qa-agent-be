# AI-105 — LLM and Outbound Email Configuration

## Purpose

Define how the backend reads the Gemini API key and shared outbound email
credentials, how an Admin can check that those integrations work, and how the
email rate is exposed for the account-import workflow.

## Scope and confirmed team decisions

The Jira story originally describes editing these settings from an Admin
dashboard. The team has since agreed on the following first version:

- Each developer provides their own Gemini API key through the backend `.env`.
- The group uses one shared sender address and Gmail App Password through the
  backend `.env`.
- The LLM model is fixed in backend code as `models/gemini-3.8-flash`.
- SMTP is fixed in backend code as Gmail `smtp.gmail.com:587` using STARTTLS.
- The configured email rate is 20 messages per minute. AI-31's account-import
  email queue is responsible for enforcing this limit; AI-105 exposes the value
  but does not implement bulk sending or queueing.
- Secrets are never returned by an API or displayed as plaintext. Changing `.env`
  credentials requires restarting the backend. Changing code-fixed values
  requires rebuilding/redeploying the backend image.

This decision defers editable dashboard configuration and database/secret-manager
storage. The Admin page only reports safe configuration metadata and provides
connection-test actions.

## Configuration

| Value | Source | Use |
|---|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Per-developer backend `.env` | Authenticates Gemini model checks and later model requests. |
| `SMTP_SENDER_EMAIL` | Shared backend `.env` | Sender address for test and outbound account/ticket email. |
| `SMTP_APP_PASSWORD` | Shared backend `.env` | Gmail App Password used for SMTP authentication; never use a normal Gmail password. |
| LLM model | Fixed in `app/config/config.py` | `models/gemini-3.8-flash`. |
| SMTP host, port, security | Fixed in `app/config/config.py` | `smtp.gmail.com`, `587`, `STARTTLS`. |
| Email rate | Fixed in `app/config/config.py` | 20 messages per minute; enforced by the AI-31 queue when integrated. |

The `.env.example` file contains empty placeholders only. Real API keys and
passwords must remain local and must not be committed.

## Backend API design

All routes require an authenticated Admin. A missing/invalid token returns `401`;
an authenticated non-Admin receives `403`.

| Method and path | Purpose | Secret handling |
|---|---|---|
| `GET /api/admin/settings` | Return the active model, SMTP host/port/security, sender address, rate, and booleans indicating whether credentials are configured. | Never returns the Gemini key or SMTP App Password. |
| `POST /api/admin/llm/test-connection` | Check whether the configured Gemini key can access the fixed model. It lists accessible models and does not generate an answer. | Returns only a success flag and safe message. |

Connection-test response shape:

```json
{"success": true, "message": "Gemini connection successful"}
```

Failure responses use a safe message and must not include provider details,
credentials, or secret values.

## Backend design and responsibilities

- `app/config/config.py` loads and validates environment-based secrets and holds
  the code-fixed model, SMTP transport, and rate values.
- `app/schemas/admin.py` defines safe response contracts; secret fields are not
  part of these schemas.
- `app/services/admin_settings_service.py` builds the read-only settings response.
- `app/services/llm_service.py` checks Gemini access without generating content.
- `app/services/email_service.py` sends the Admin test message and provides the
  shared plain-text email sender for later account/ticket email integration.
- `app/routers/admin/router.py` exposes the Admin-only endpoints and returns
  sanitized results.

The AI-31 account-import flow remains responsible for parsing users, queueing
account emails, retry policy, and enforcing the 20-per-minute delivery limit.
Integration must reuse the shared sender rather than create a second SMTP
configuration path.

## Local setup and verification

1. Copy `.env.example` to `.env` in the backend repository.
2. Set a personal `GEMINI_API_KEY` and the group's `SMTP_SENDER_EMAIL` and
   `SMTP_APP_PASSWORD`. Use a Gmail App Password, not the account password.
3. Restart the backend after changing `.env` values.
4. Sign in as an Admin and open `/admin/settings`.
5. Confirm the UI reports whether each credential is configured, then run
   **Test connection**. Email delivery is exercised by queuing real mail and
   using **Send pending emails** on `/admin/email`.

Automated checks (run from the backend repository root):

```powershell
uv run python -m pytest tests/admin/ tests/config/ tests/email/ tests/llm/ -q
uv run ruff check .
```

These tests mock Gemini and SMTP. They do not send real mail or consume Gemini
API quota. Bulk rate enforcement is verified with the AI-31 queue integration,
not by the AI-105 connection-test endpoint.

## Acceptance-criteria mapping

| Jira criterion | Agreed implementation/status |
|---|---|
| AC-01: configure Gemini key securely; each member can use their own key | Each developer supplies a key via local/backend deployment environment. The key is secret-typed, omitted from API responses, and not editable in the dashboard in this version. |
| AC-02: choose model name | Model is fixed in backend code to `models/gemini-3.8-flash`, per team decision; dashboard model selection is deferred. |
| AC-03: test connection | Admin-only endpoint and Settings UI test access to the configured Gemini model without generation. |
| AC-04: mask key and never log plaintext | API contract returns only a configured boolean; secrets are not returned. |
| AC-05: configure outbound email sender | Shared sender address and Gmail App Password come from environment; Gmail STARTTLS host/port are code-fixed. |
| AC-06: protect app password | Password is secret-typed and omitted from API responses; UI reports only whether it is configured. |
| AC-07: test email and configure send rate | Admin can send a test email to their own authenticated address. The rate is fixed at 20/minute and exposed by settings; queue enforcement belongs to AI-31. |

## Branch and merge-request convention

Keep the design and implementation deliverables separate:

- Design branch: `features/Design_ConfigureLLMApiKeyEmailSender`; contains this
  document under `docs/design/`.
- Implementation branch: `features/Implementation_ConfigureLLMApiKeyEmailSender`;
  contains backend/frontend code and tests, but not this design document.

Create separate merge requests for the two branches and link both to AI-105. Do
not mark review, CI, or manual-test checklist items complete without evidence.

## Out of scope

- Editing credentials or model settings through the dashboard.
- Storing these secrets in the application database.
- Bulk Excel import, account creation, email queue/retry behavior, and rate-limit
  enforcement (AI-31).
