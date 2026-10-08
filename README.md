# course-qa-agent-be

Backend service for the course Q&A RAG chatbot — FastAPI modular monolith.
Split from the `course-qa-agent` monorepo (project skeleton AI-112).

- Frontend repo: `course-qa-agent-fe`
- Jira: AI project on `ai03.atlassian.net` — this repo maps to [AI-112](https://ai03.atlassian.net/browse/AI-112)

## Stack

- Python 3.11+ with [uv](https://docs.astral.sh/uv/) for dependency management
- FastAPI + pydantic-settings (all thresholds and connection strings in `app/config.py`, never hardcoded)
- Layered modular monolith: `app/main.py`, `app/config.py`, `app/routers/`, `app/schemas/`, `app/services/` — one module per Epic E1–E8
- Tests: pytest + httpx (FastAPI `TestClient`); lint: ruff

## Structure

```
.
├── app/
│   ├── main.py          # FastAPI entrypoint — /health, router registration
│   ├── config.py        # pydantic-settings (DATABASE_URL, QDRANT_URL, CORS)
│   ├── core/            # cross-cutting: security, deps (later sprints)
│   ├── models/          # SQLAlchemy models (later sprints)
│   ├── routers/         # one package per Epic: auth, documents, chat, ...
│   ├── schemas/         # pydantic request/response models
│   └── services/        # business logic
├── tests/               # pytest suite (test_health.py)
├── pyproject.toml       # deps + ruff + pytest config
├── uv.lock              # locked dependencies — commit on every dep change
└── docker-compose.yml   # Postgres + Qdrant + backend (added in docker commit)
```

## Local run

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

Health check:

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"course-qa-agent-backend"}
```

## Tests

```bash
uv run pytest -q      # unit tests (pythonpath configured in pyproject)
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

## Docs

API design: [`docs/api-design-health.md`](docs/api-design-health.md) — lands with
the Design branch (`features/Design_ProjectSkeleton`, MR !1).
