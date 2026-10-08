# backend/app/services — application/domain layer

Business logic, one service per Epic (Modular Monolith + Layered, TECHSTACK §8). Routers call services; services never import routers.

| Planned service | Epic | Jira | Contents |
|---|---|---|---|
| `auth_service.py` | E1 | AI-25/27/29/31 | login, JWT, RBAC checks, bulk import + email queue |
| `document_service.py` | E2 | AI-35/37/39/41/43 | upload validation (MIME), list/search, class tags, delete + Qdrant cleanup |
| `ingestion_service.py` | E3 | AI-47/49/51/53/55 | pipeline: extract → Vision → chunk → embed → Qdrant upsert; approval + retry (idempotent) |
| `rag_service.py` | E4 | AI-61/63/65/67 | retrieve top-k (class filter) → threshold → generate / web_search agent, SSE stream |
| `citation_service.py` | E5 | AI-73/75/77 | build citation payload from chunk metadata, serve excerpts |
| `ticket_service.py` | E6 | AI-81–89 | student-initiated tickets only — **no auto-create path** |
| `gap_service.py` | E6 | AI-91 | write to `content_gap_queries`, cluster via Qdrant vector_search, TTLCache (30–60 min, key includes config version) |
| `history_service.py` | E7 | AI-95/97/99/101 | save/load/search/delete conversations; delete cascades to gap collection |

**Coordination rules (7 members, one codebase)**
- Each story lands in its own service file → minimal merge conflicts.
- Shared helpers go to `core/` only if 2+ services need them.
- Cross-service flow (upload → ingest → index) is orchestrated by the calling service, not by import cycles between services.
