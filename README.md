# course-qa-agent-be

Backend service for the course Q&A RAG chatbot — FastAPI modular monolith.
Split from the `course-qa-agent` monorepo (project skeleton AI-112).

- Frontend repo: `course-qa-agent-fe`
- Jira: AI project on `ai03.atlassian.net` — this repo maps to [AI-112](https://ai03.atlassian.net/browse/AI-112)

## Stack

- Python 3.11+ with [uv](https://docs.astral.sh/uv/) for dependency management
- FastAPI + pydantic-settings (all thresholds and connection strings in `app/config/`, never hardcoded)
- Layered modular monolith: `app/main.py`, `app/config/`, `app/routers/`, `app/schemas/`, `app/services/` — one module per Epic E1–E8
- Tests: pytest + httpx (FastAPI `TestClient`); lint: ruff

## Structure

```
.
├── app/
│   ├── main.py          # FastAPI entrypoint — /health, router registration
│   ├── config/          # pydantic-settings package (DATABASE_URL, QDRANT_URL, CORS, ...)
│   ├── core/            # cross-cutting: security, deps (later sprints)
│   ├── models/          # SQLAlchemy models (later sprints)
│   ├── routers/         # one package per Epic: auth, documents, chat, ...
│   ├── schemas/         # pydantic request/response models
│   └── services/        # business logic
├── tests/               # pytest suite organized by feature area
├── pyproject.toml       # deps + ruff + pytest config
├── uv.lock              # locked dependencies — commit on every dep change
├── Dockerfile           # multi-stage: uv builder -> python:3.12-slim runtime
├── .dockerignore
└── docker-compose.yml   # Postgres + Qdrant + backend
```

## Local run

```bash
uv sync
docker start course-qa-postgres course-qa-qdrant  # infra only
uv run alembic upgrade head      # schema (idempotent; existing DBs: alembic stamp head once)
uv run python -m app.core.seed --dev  # roles + 3 accounts + TEST101 course/class/enrollment
uv run uvicorn app.main:app --reload --port 8000
```

Health check:

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"course-qa-agent-backend"}
```

Test accounts (seeded): `admin@academic.edu.vn` / `Admin@123`,
`teacher@academic.edu.vn` / `Teacher@123` (owns TEST101-A),
`student@academic.edu.vn` / `Student@123` (enrolled in TEST101-A).

## Docker (whole stack)

One command — no manual uvicorn:

```bash
docker compose up -d --build
```

| Service | Host port | Container port | Notes |
|---|---|---|---|
| backend (uvicorn) | 8000 | 8000 | `GET /health`, container `course-qa-backend` |
| PostgreSQL 16 | 5432 | 5432 | user `qa` / password `qa` / db `course_qa`, container `course-qa-postgres` |
| Qdrant REST | 6333 | 6333 | `GET /healthz` -> 200, container `course-qa-qdrant` |
| Qdrant gRPC | 6334 | 6334 | — |

**Port rule — local first, Docker second:** both modes use the **same** ports,
so run **either** local processes **or** containers, never both at once.

| Mode | Command | Backend port |
|---|---|---|
| Local (develop) | `uv run uvicorn app.main:app --reload --port 8000` | 8000 |
| Docker (whole stack) | `docker compose up -d --build` | 8000 |

Volumes keep the monorepo names (`course-qa-agent_postgres_data`,
`course-qa-agent_qdrant_storage`) so existing data is preserved.
Stop with `docker compose down` (add `-v` only to wipe data).

### Schema changes & migrations

Schema is owned by Alembic only. The backend container runs
`alembic upgrade head` on every start (`docker-entrypoint.py`) and exits with a
clear error if it cannot migrate — startup no longer creates tables.

- After pulling code with new migrations: `docker compose up -d --build` is enough.
- Running the backend outside Docker: always `uv run alembic upgrade head` first.
- If a local DB was created by the old `create_all` startup (new tables exist but
  new columns are missing, or Alembic reports `DuplicateTable`), reset the local
  volumes once: `docker compose down -v && docker compose up -d --build`
  (destroys local data; seeding re-creates the demo accounts).

## Tests

```bash
uv run pytest -q      # unit tests (discovered recursively; pythonpath configured in pyproject)
uv run ruff check .   # lint
```

## Configuration

Copy `.env.example` to `.env` and fill in values. Never commit real keys.
Each developer uses their own `GEMINI_API_KEY` (free-tier rate limits).

| Variable | Default (local) |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://qa:qa@localhost:5432/course_qa` |
| `QDRANT_URL` | `http://localhost:6333` |
| `APP_ENV` | `local` |
| `CORS_ORIGINS` | `["http://localhost:3000"]` |
| `GEMINI_API_KEY` | unset; each developer supplies their own key (`GOOGLE_API_KEY` is also accepted) |
| `JWT_SECRET` | `dev-only-secret-change-me-use-env-in-prod` (change in prod) |
| `JWT_EXPIRE_MINUTES` | `720` |
| `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` | first admin on fresh DB |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_SENDER_EMAIL` | Gmail sender config (AI-105, AC-08); `SMTP_SENDER` accepted as legacy alias |
| `SMTP_APP_PASSWORD` | Gmail **app** password — never a personal password |
| `SMTP_RATE_PER_MINUTE` | `20` (queue rate limit, AC-09) |
| `EMAIL_DRY_RUN` | `true` (no real SMTP until configured) |

AI-105 uses the fixed Gemini model `models/gemini-3.8-flash` and Gmail SMTP over
STARTTLS on port `587`. Changing environment credentials requires restarting the
local backend; Docker reads `.env` when the container is created, so rebuild/recreate
the backend after changing its environment. Model or SMTP transport defaults are
code-fixed and require a code change plus a Docker rebuild.

Admin endpoints require an `Authorization: Bearer <token>` for an Admin role:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/admin/settings` | Read model/sender details and whether secrets are configured; secret values are never returned. |
| `POST` | `/api/admin/llm/test-connection` | Check the configured Gemini key and fixed model. |
| `POST` | `/api/admin/email/test` | Send a test email to the authenticated Admin's email address. |

Open `http://localhost:8000/docs` to inspect and try the API contracts. The agreed
email rate is **20 per minute** and is exposed in settings; enforcement belongs to
the AI-31 bulk-import queue (`POST /api/admin/email-queue/process` with rate limit).
By team decision, API keys and SMTP credentials are changed in `.env`, not sent to
or updated through the AI-105 settings API (the AI-31 outbox config in DB is the
only runtime-updatable email config).

## Docs

API design: [`docs/api-design-health.md`](docs/api-design-health.md) — lands with
the Design branch (`features/Design_ProjectSkeleton`, MR !1).
