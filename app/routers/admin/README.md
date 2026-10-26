# Admin settings API — AI-105

AI-105's backend endpoints expose safe configuration status and let an authenticated
Admin test the configured Gemini and SMTP connections. They do not accept or return
secret values.

## Team configuration decisions

- Each developer provides their own `GEMINI_API_KEY` in the backend `.env`.
- The shared sender address and Gmail App Password are set with
  `SMTP_SENDER_EMAIL` and `SMTP_APP_PASSWORD` in the backend `.env`.
- The Gemini model (`models/gemini-3.8-flash`) and Gmail SMTP transport
  (`smtp.gmail.com:587`, STARTTLS) are fixed in backend code.
- Changing credentials requires restarting the backend. Changing code-fixed values
  also requires rebuilding/redeploying the backend image.
- The agreed bulk email rate is 20 messages per minute. This API reports the rate;
  enforcing it belongs to the AI-31 email outbox queue integration.

## Endpoints

All endpoints require `Authorization: Bearer <token>` for an Admin role. A missing
or invalid token returns `401`; an authenticated non-Admin receives `403`.

| Method | Endpoint | Behavior |
|---|---|---|
| `GET` | `/api/admin/settings` | Returns the active chat model, SMTP metadata, rate, and boolean secret-readiness flags. Secret values are omitted. |
| `GET` | `/api/admin/llm/keys` | Lists keys with masked value, status, cooldown, last-used time, last error, and the auto-switch setting. |
| `POST` | `/api/admin/llm/keys` | Adds an encrypted key and validates it against the configured chat model. |
| `PATCH` | `/api/admin/llm/keys/{key_id}` | Renames, updates optional metadata, or pauses/resumes a key. |
| `DELETE` | `/api/admin/llm/keys/{key_id}` | Removes a key from the pool. |
| `POST` | `/api/admin/llm/keys/{key_id}/test` | Checks that one key can access the configured chat model. |
| `PATCH` | `/api/admin/llm/auto-switch` | Enables or disables automatic rotation to the next healthy key. |
| `GET` | `/api/admin/llm/models` | Lists chat models available to active keys. |
| `PATCH` | `/api/admin/llm/model` | Updates the chat model used for new answers. |
| `POST` | `/api/admin/llm/test-connection` | Checks the environment `GEMINI_API_KEY` against the configured model. |
| `POST` | `/api/admin/email/test` | Sends a plain-text test email to the email address in the authenticated Admin token. No request body is needed. |

Successful and failed connection checks return the same shape:

```json
{"success": true, "message": "Gemini connection successful"}
```

Failure results use a safe human-readable message and do not include provider
details, API keys, or SMTP passwords.

## Configure and verify locally

1. Copy `.env.example` to `.env`, set `GEMINI_KEY_ENCRYPTION_KEY`, and fill in the
   shared sender credentials. Use a Gmail App Password, not the account's normal
   password.
2. Restart the backend so it reads the environment values.
3. Sign in as an Admin, open `/admin/settings`, and add at least one Gemini key.
4. Use the per-key test action for Gemini and **Send test email** for SMTP. The
   email check sends to the signed-in Admin's email address.

Automated verification:

```powershell
uv run python -m pytest tests/admin/ tests/config/ tests/email/ tests/llm/ -q
uv run ruff check .
```

The automated tests mock Gemini and SMTP; they do not send real email or consume
Gemini API quota. Bulk email delivery/rate enforcement is not claimed until AI-31's
queue is integrated with the agreed 20-per-minute setting.
