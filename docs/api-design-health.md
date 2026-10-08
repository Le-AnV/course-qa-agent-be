# API Design — Health Check (AI-112)

> Design deliverable for `features/Design_ProjectSkeleton` — companion to the
> Implementation branch. Related: [AI-112](https://ai03.atlassian.net/browse/AI-112).
> Base URL (local): `http://localhost:8000`

## GET /health

Readiness probe used by Docker healthchecks, the frontend home page, and future
monitoring. No authentication (public endpoint, no side effects).

### Request

```http
GET /health HTTP/1.1
Host: localhost:8000
Accept: application/json
```

No path, query, or body parameters.

### Success response — `200 OK`

```json
{
  "status": "ok",
  "service": "course-qa-agent-backend"
}
```

| Field | Type | Description |
|---|---|---|
| `status` | string | `"ok"` while the process is serving requests |
| `service` | string | Fixed service identifier, `"course-qa-agent-backend"` |

### Error responses

None defined for v1 — endpoint does not touch the database. If later sprints
extend it to check Postgres/Qdrant, add:

| Status | Body | When |
|---|---|---|
| `200` | `{"status":"ok", "dependencies":{"postgres":"up","qdrant":"up"}}` | all dependencies reachable |
| `503` | `{"status":"degraded", "dependencies":{"postgres":"down","qdrant":"up"}}` | any dependency unreachable |

### Example (curl)

```bash
$ curl http://localhost:8000/health
{"status":"ok","service":"course-qa-agent-backend"}
```

### Example (frontend, TypeScript)

```ts
const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/health`);
const body = await res.json(); // { status: "ok", service: "course-qa-agent-backend" }
```

## Infrastructure ports (docker-compose.yml)

| Service | Host port | Container port | Health probe |
|---|---|---|---|
| Backend (uvicorn, run on host) | 8000 | — | `GET /health` |
| Frontend (next dev, run on host) | 3000 | — | — |
| PostgreSQL 16 | 5432 | 5432 | `pg_isready -U qa -d course_qa` |
| Qdrant REST | 6333 | 6333 | `GET /healthz` → 200 |
| Qdrant gRPC | 6334 | 6334 | — |

## Conventions for future endpoints

- Prefix resources with version-free nouns: `/documents`, `/chat`, `/tickets`
  (routers registered in `app/main.py` per Epic E1–E8).
- Responses are JSON objects; errors use FastAPI's default
  `{"detail": "..."}` shape.
- Auth (JWT) applies to every endpoint **except** `/health` — enforced in
  FastAPI, not Next.js.
