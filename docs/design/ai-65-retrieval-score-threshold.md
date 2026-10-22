# AI-65 — Retrieval Score Threshold (KB vs Internet)

> **Ticket:** AI-65 (Epic E4, parent AI-23) · **Status:** Implemented (backend + admin UI)
> **Branch:** `features/Implementation_RetrievalScoreThreshold`
> **Related stories:** AI-61 (RAG Q&A backend), AI-67 (Internet source agent),
> AI-107 (embedding/retrieval configuration), AI-39 (deleted/replaced documents).

## Purpose

Every student question must get an answer from a **cited source**. Retrieval
runs first; if the best class-material score is confident enough, the answer is
generated from the curriculum (AC-02). Otherwise the question is handed to the
Internet source agent (AC-03). The model never answers from its own knowledge
(AC-04), and an admin can change the confidence threshold without a redeploy
(AC-05).

## Scope and confirmed team decisions

| Decision | Rationale |
|---|---|
| Core routing lives in `app/services/rag_service.py` | Matches `app/routers/chat/README.md`; AI-61 will call it from the streaming endpoint. |
| Retrieval always runs before the routing decision | AC-01 is guaranteed structurally: `decide_source()` calls `retrieve_class_chunks()` before any comparison. |
| Comparison is inclusive (`score >= threshold`) | AC-02: a score exactly on the threshold still answers from the materials. |
| Default threshold is `0.8` | `settings.retrieval_threshold` (AI-107). No environment variable needed. |
| The active threshold is stored in `system_config` under `retrieval_threshold` | AC-05 / T35. DB value overrides the code default; the next question reads it. |
| Retrieval is class-scoped in Qdrant | `QdrantStore.similarity_search_with_score()` filters on the `class_id` payload written by AI-41/39, so another class can never leak into an answer. |
| AI-39's live-document guard is applied after retrieval | A deleted/replaced file whose vectors were not fully purged can never win the threshold or be cited. |
| The Internet provider is a stub | AI-67 owns T19 and the provider is still TBD (TECHSTACK §7.4). The stub returns no results instead of inventing them. |
| No source → deterministic message, no LLM call | AC-04 forbids answering from model knowledge. The request still returns normally (not a ticket/refusal, AC-03). |
| Answer generation only receives numbered sources | Curriculum uses `answer-course-prompt`; Internet uses `answer-internet-prompt`. Both forbid outside knowledge. |

### AC-03 vs AC-04 tension

AC-03 says the Internet agent "always answers", while AC-04 forbids answers
without a source. If the agent returns no usable source (provider down, no
results), the only consistent behaviour is a normal, truthful message
(`NO_SOURCE_MESSAGE`) with `used_web_search=true` and zero citations. The
request is still accepted — no error, no refusal, no ticket — but the LLM is
never asked to invent an answer. When AI-67 provides a working provider, the
normal path always carries Internet citations.

## API design

### `GET /api/admin/retrieval-config` (admin only)

Returns the active threshold and the documented default.

```json
{
  "threshold": 0.8,
  "default_threshold": 0.8,
  "is_default": true,
  "updated_at": null
}
```

### `PATCH /api/admin/retrieval-config` (admin only)

```json
{ "threshold": 0.65 }
```

Response is the same shape as `GET`, with `is_default: false` and the new
`updated_at`. Values outside `[0, 1]` are rejected with `422`; a trainer or
student receives `403`; an anonymous caller receives `401`.

### Service contract (used by AI-61)

| Function | Purpose |
|---|---|
| `get_retrieval_threshold(db)` | Active threshold; falls back to `0.8` when unset/corrupt. |
| `save_retrieval_threshold(db, value)` | Validate and persist; raises `RetrievalThresholdError`. |
| `retrieve_class_chunks(db, question=..., class_id=..., store=...)` | Class-scoped scored retrieval + AI-39 live-document guard. |
| `decide_source(db, question=..., class_id=..., store=...)` | Returns `RetrievalDecision(source, threshold, top_score, chunks)`. |
| `answer_question(db, question=..., class_id=..., internet_search=..., generate=...)` | Full routing + grounded generation + E5 citations. |

`QdrantStore.similarity_search_with_score(query, k=..., class_id=...)` is the
new scored retrieval primitive (retry policy matches the existing search).

## Acceptance criteria status

| AC | Status | Evidence |
|---|---|---|
| AC-01: retrieval runs first; top score compared with the configured threshold (default `0.8`) | Implemented | `decide_source()` retrieves before comparing; `test_retrieval_always_runs_class_scoped_before_routing`, `test_threshold_defaults_to_documented_0_8`. |
| AC-02: `score >= threshold` → answer from class materials with curriculum citations (E5) | Implemented | `answer_question()` curriculum path; `test_score_equal_to_threshold_answers_from_curriculum`, `test_curriculum_answer_carries_citations`. |
| AC-03: `score < threshold` → hand to the Internet agent; never refused or turned into a ticket | Implemented (agent stubbed) | `test_score_below_threshold_hands_question_to_internet_agent`, `test_retrieval_outage_falls_back_to_internet_instead_of_refusing`. AI-67 replaces `search_internet`. |
| AC-04: never answer from the LLM's own knowledge; a source is required | Implemented | Strict prompts + no-source short-circuit; `test_internet_agent_outage_returns_no_source_message`, `test_generation_failure_returns_only_cited_sources`. |
| AC-05: threshold configurable by an admin, not hardcoded (T35) | Implemented | `GET/PATCH /api/admin/retrieval-config`, `system_config.retrieval_threshold`; `test_threshold_is_persisted_and_applies_to_next_question`, `test_threshold_change_switches_the_same_score_between_sources`, admin/non-admin API tests. |

## How to verify

Backend:

```powershell
uv run python -m pytest tests/services/rag/ -q
uv run python -m pytest tests/services/vector_store/ -q
uv run ruff check .
```

Frontend (admin card):

```powershell
npx vitest run lib/admin-api.test.ts
npx eslint "lib/admin-api.ts" "app/(shell)/admin/settings/page.tsx"
```

Manual check with a running stack:

1. Log in as an admin and open **Admin → Settings**.
2. Change **Answer routing** from `0.80` to `0.65`; the badge switches to `Custom`.
3. Ask a question whose top match scores between `0.65` and `0.80`: it should
   now answer from the class materials with citations. Set it back to `0.80`
   and the same question should go to the Internet agent.
4. Delete a cited document (AI-39) and repeat the question: the deleted file
   must not appear in the citations.

## Not included in AI-65

- The streaming `POST /api/chat` endpoint and its UI (AI-61 / AI-69).
- The real Internet search provider (AI-67); the stub is intentional.
- Content-gap counting/grouping (AI-68) and automatic threshold tuning.
- Changing `retrieval_threshold` from an environment variable; the admin API
  is now the supported override, and the AI-107 code default remains `0.8`.

## Integration notes

- **AI-61** should call `answer_question()` and stream `QaAnswer.answer`; the
  `citations` tuple already carries the E5 fields (`kind`, `title`, `url`,
  `page`, `document_id`, `snippet`).
- **AI-67** only needs to provide a callable returning `WebResult` objects and
  pass it as `internet_search=`; the dispatcher already handles failures and
  empty results.
- **AI-39** guard: chunks are kept only while their document is `processing` or
  `indexed`; unverifiable chunks are dropped.
