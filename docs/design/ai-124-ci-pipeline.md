# CI Pipeline Design — BE Repo (AI-124)

> Design deliverable for `features/Design_CIPipelineBE` — companion to
> `features/Implementation_CIPipelineBE`. Related: [AI-124](https://ai03.atlassian.net/browse/AI-124).
> No endpoint changes in this ticket: the deliverable is a **CI configuration
> file**, so this document is the spec and the green pipeline on the
> Implementation MR is the acceptance evidence.

## 1. Goals / Non-goals

**Goals**

- Every MR into `course-qa-agent-be` is automatically verified by `lint` + `test`
  on the project self-hosted runner — the machine catches breakages, reviewers
  review logic.
- Both jobs are hard gates: the repo is clean today (`ruff` passes, `pytest`
  passes), so there is no reason for advisory mode.
- Pipeline triggers only on MR events and pushes to `main` — no duplicate runs.

**Non-goals**

- No CD, no container image push, no deployment of any kind.
- No real secrets in `.gitlab-ci.yml` — the test suite needs no database
  (`test_health.py` uses FastAPI `TestClient` only).
- No changes to application code, Dockerfile, or dependencies.

## 2. Pipeline layout

Runner: project self-hosted runner #757 (`course-qa-BE`, shell executor,
Windows) — no tags pinned initially, so the job is picked up untagged. (If jobs
stay pending, pin `tags` to match the runner afterwards, same as FE AI-123.)

| Stage | Job | Image | Command | Gate |
|---|---|---|---|---|
| `lint` | `lint` | `python:3.12-slim` | `uv sync --frozen` → `uv run ruff check .` | hard (must pass) |
| `test` | `test` | `python:3.12-slim` | `uv sync --frozen` → `uv run pytest -q` | hard (must pass) |

Rules:

```yaml
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == "main"
```

## 3. Job details

**`lint`** — `uv sync --frozen` installs locked dependencies (including the
`dev` group: `pytest`, `httpx`, `ruff`). Runs `uv run ruff check .` (config in
`pyproject.toml`: `target-version = "py312"`, `line-length = 100`). Any new
violation fails the pipeline — this keeps the repo clean from day one instead
of accumulating lint debt like FE did (MR !3).

**`test`** — same install step, then `uv run pytest -q` (`testpaths = ["tests"]`,
`pythonpath = ["."]`). Current suite is one DB-free test (`test_health.py`:
`GET /health` → 200 + `{"status": "ok", "service": "course-qa-agent-backend"}`),
so no services (postgres/qdrant) are needed. When DB-backed tests arrive, add a
`services:` section or run against docker compose — see open items.

**Not included:** a `docker build` check — the shell runner has no Docker
daemon. Revisit if a Docker-capable runner becomes available. Local
`docker compose up` remains the manual verification for the image.

## 4. Why both jobs are hard gates

Unlike FE (AI-123), where `lint` is advisory because `main` carries ESLint
errors from MR !3 (Hao's AI-25 scope), BE `main` was verified clean on
2026-10-08: `uv run ruff check .` → `All checks passed!`, `uv run pytest -q`
→ `1 passed`. Both gates stay hard — any future violation is new debt from the
MR under review, and the author fixes it in that MR.

## 5. Verification

On the Implementation MR (`features/Implementation_CIPipelineBE` → `main`):
open the MR and confirm the pipeline shows `lint` green and `test` green.
Logs of the green pipeline are the acceptance evidence for AI-124.

## 6. Open items

1. Pin runner `tags` if jobs stay pending on the self-hosted runner.
2. Add a `docker build` check if a Docker-capable runner becomes available.
3. When DB-backed tests arrive: add postgres/qdrant services to the `test` job
   (or point tests at docker compose) — current suite needs neither.
