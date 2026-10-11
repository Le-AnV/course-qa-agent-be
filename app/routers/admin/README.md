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
| `GET` | `/api/admin/settings` | Returns the active model, SMTP metadata, rate, and boolean secret-readiness flags. Secret values are omitted. |
| `POST` | `/api/admin/llm/test-connection` | Checks that the configured Gemini key can access the fixed model. Does not generate an answer. |
| `POST` | `/api/admin/email/test` | Sends a plain-text test email to the email address in the authenticated Admin token. No request body is needed. |

Successful and failed connection checks return the same shape:

```json
{"success": true, "message": "Gemini connection successful"}
```

Failure results use a safe human-readable message and do not include provider
details, API keys, or SMTP passwords.

## Configure and verify locally

1. Copy `.env.example` to `.env` and fill in the local Gemini key and shared sender
   credentials. Use a Gmail App Password, not the account's normal password.
2. Restart the backend so it reads the environment values.
3. Sign in as an Admin, open `/admin/settings`, and check that each credential shows
   as configured.
4. Use **Test connection** for Gemini and **Send test email** for SMTP. The email
   check sends to the signed-in Admin's email address.

Automated verification:

```powershell
uv run python -m pytest tests/test_admin_router.py tests/test_admin_schemas.py tests/test_admin_settings_service.py tests/test_config.py tests/test_email_service.py tests/test_llm_service.py -q
uv run ruff check .
```

The automated tests mock Gemini and SMTP; they do not send real email or consume
Gemini API quota. Bulk email delivery/rate enforcement is not claimed until AI-31's
queue is integrated with the agreed 20-per-minute setting.
