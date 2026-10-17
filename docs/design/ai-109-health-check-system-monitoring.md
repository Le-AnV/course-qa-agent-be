# AI-109 — Health Check & System Monitoring

## Purpose

Give the team one place to see whether the backend and its runtime dependencies —
PostgreSQL, Qdrant, and the Google Gemini API — are healthy, and give the Admin
dashboard a live view of the same three components so outages are detected
quickly.

## Scope and confirmed team decisions

- Three components are monitored: `db` (PostgreSQL), `qdrant` (vector store), and
  `llm` (Gemini API).
- Health is exposed on the existing public `GET /health` (used by Docker, the
  frontend, and uptime monitors) and on a new Admin endpoint
  `GET /api/admin/system/health` for the dashboard.
- The public `/health` path must stay cheap. It probes DB and Qdrant live but
  reports the LLM by *configuration readiness* only, so the 30-second Docker
  healthcheck never calls Gemini.
- The Admin path performs a *live* Gemini probe. Its result is cached for a short
  TTL (60s) so dashboard auto-refresh does not spam the provider.
- `/health` always returns HTTP 200; the body carries the per-component status so
  an orchestrator can read it. The Admin endpoint enforces RBAC.
- Errors are logged without secrets (AC-03): provider exceptions are reduced to a
  safe message and never include keys.

## Backend API design

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /health` | public | Fast component health for Docker/uptime; LLM reported by configuration only. |
| `GET /api/admin/system/health` | Admin | Live component health (DB, Qdrant, Gemini) for the dashboard. |

Both endpoints return the same contract:

```json
{
  "status": "ok",
  "service": "course-qa-agent-backend",
  "components": [
    {"name": "db",     "status": "up",         "healthy": true, "detail": "PostgreSQL reachable"},
    {"name": "qdrant", "status": "up",         "healthy": true, "detail": "Qdrant reachable"},
    {"name": "llm",    "status": "configured", "healthy": true, "detail": "Gemini API key configured"}
  ]
}
```

- `ComponentStatus` = `up | down | configured | unconfigured`.
  - `up` / `down` — live probe result (DB, Qdrant, and Gemini on the Admin path).
  - `configured` / `unconfigured` — configuration readiness only, used by the fast
    `/health` path for the LLM.
- `SystemStatus` (`status`) = `ok` when every component is healthy, otherwise
  `degraded`.
- `healthy` is the boolean each component contributes to the roll-up.

A missing or invalid token on the Admin endpoint returns `401`; an authenticated
non-Admin receives `403`. `/health` stays anonymous.

## Backend design and responsibilities

- `app/schemas/system_health.py` — `ComponentHealth`, `SystemHealthRead`,
  `ComponentStatus`, `SystemStatus`.
- `app/services/system_health_service.py` — `build_health(db, config, *, live_llm)`
  probes DB (`SELECT 1` on the request session), Qdrant (`get_collections` with a
  3s timeout), and the LLM, then rolls the results into one `SystemHealthRead`.
  `live_llm=False` uses the cheap configuration check; `live_llm=True` performs the
  live probe whose result is cached for `LLM_CACHE_TTL_SECONDS` (60s) behind a
  lock. `reset_llm_cache()` clears the cache (tests / explicit refresh).
- `app/services/llm_service.py` — `check_llm_connection` lists accessible models
  (no generation) and maps failures to safe `LlmConfigurationError` /
  `LlmConnectionError`.
- `app/main.py` — `GET /health` depends on `get_db` and calls
  `build_health(..., live_llm=False)`.
- `app/routers/admin/router.py` — `GET /api/admin/system/health` behind
  `require_admin`, calls `build_health(..., live_llm=True)`.

## Test coverage

- `tests/test_health.py` — fast path: ok/degraded roll-up, LLM reported by
  configuration, DB down → degraded.
- `tests/admin/test_system_health.py` — service level: up/down per component, LLM
  cache TTL, secret-free `detail`, `reset_llm_cache`.
- `tests/admin/test_admin_router.py` — the Admin endpoint is added to the RBAC
  matrix (401 without token, 403 for non-admin roles).

Run from the backend repository root:

```powershell
uv run python -m pytest tests/test_health.py tests/admin/ -q
uv run ruff check .
```

## Local setup and verification

1. Set a personal `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in the backend `.env`.
2. Restart the backend after changing `.env`. `env_file` values are only injected
   when the container is (re)created, so a running container keeps the old
   environment: `docker compose up -d --force-recreate backend`.
3. Check the fast path: `GET /health`.
4. Sign in as Admin, open `/admin/system`, and confirm all three components are
   green; that page uses the live Admin endpoint.

The dashboard reports a red LLM when the key is missing/blank or the Gemini API is
unreachable. All `detail` strings are safe to display and are logged without
secrets.

## Acceptance-criteria mapping

| Jira criterion | Agreed implementation |
|---|---|
| AC-01: `/health` returns the status of each component (DB, Qdrant, LLM API) | `GET /health` returns a `components[]` array with the status of `db`, `qdrant`, and `llm`, plus a rolled-up `status`. DB and Qdrant are probed live; the LLM is reported by configuration readiness to keep the Docker healthcheck cheap. The live Gemini probe is available on `GET /api/admin/system/health`. |
| AC-02: The dashboard shows green/red for each component | Admin endpoint `GET /api/admin/system/health` returns the same per-component status (`healthy` boolean) that the frontend `/admin/system` page renders as green/red. |
| AC-03: Errors are logged (without secrets) | Probe failures are caught and logged with a static, secret-free message; provider errors are reduced to safe `detail` strings and never include keys or provider payloads. |

## Branch and merge-request convention

Keep the design and implementation deliverables separate:

- Design branch: `features/Design_HealthCheckSystemMonitoring`; contains this
  document under `docs/design/`.
- Implementation branch: `features/Implementation_HealthCheckSystemMonitoring`;
  contains backend code and tests, but not this design document.

Create separate merge requests for the two branches and link both to AI-109. Do
not mark review, CI, or manual-test checklist items complete without evidence.

## Out of scope

- Metrics, alerting, or historical graphs (this story reports current state only).
- Monitoring dependencies beyond the three listed (e.g. SMTP).
- Replacing the Docker healthcheck or adding new infrastructure.
