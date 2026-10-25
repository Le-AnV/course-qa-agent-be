# AI-65 — Retrieval Score Threshold (KB vs Internet vs Out of Scope)

> **Ticket:** AI-65 (Epic E4, parent AI-23) · **Status:** Implemented (3-Tier routing, fail-fast CPU optimization, admin test sandbox)
> **Branch:** `features/Implementation_RetrievalScoreThreshold`
> **Related stories:** AI-61 (RAG Q&A backend), AI-67 (Internet source agent),
> AI-107 (embedding/retrieval configuration), AI-39 (deleted/replaced documents).

## Purpose

Every student question is routed through a rigorous multi-tier decision boundary:
1. **Curriculum Source (`SOURCE_CURRICULUM`)**: When class materials contain high-confidence evidence ($Score \ge T_{high}$), answers are generated strictly grounded in course materials with explicit citations (AC-02).
2. **Internet Fallback (`SOURCE_INTERNET`)**: When class materials lack sufficient confidence but the query is still relevant ($T_{low} \le Score < T_{high}$), the question is handed to the Internet source agent (AC-03).
3. **Out of Scope / Off-Topic Guard (`SOURCE_UNRELATED`)**: When the query has no substantive connection to course topics ($Score < T_{low}$), it is rejected early, preventing hallucinated answers or irrelevant web searches.

An administrator can inspect and adjust both thresholds ($T_{high}$ and $T_{low}$) live without redeploying (AC-05), and can validate threshold behavior against class documents via an interactive test sandbox.

## Scope and Confirmed Architecture

| Decision | Rationale |
|---|---|
| 3-Tier Routing Decision in `app/services/rag_service.py` | Formulates the decision boundary based on dual thresholds: $T_{high}$ (`retrieval_threshold`, default `0.80`) and $T_{low}$ (`unrelated_threshold`, default `0.45`). |
| Fail-Fast CPU Bypass in `app/services/chat_retrieval_service.py` | If the maximum cosine candidate returned by Qdrant is already below $T_{low}$, Cross-Encoder reranking is skipped completely, cutting off-topic query latency from 5–6s to < 0.2s. |
| Preserving Cosine Similarity in `app/services/reranker_service.py` | Cross-Encoder reorders chunk candidates for optimal contextual generation, but the original normalized cosine similarity score $[0.0, 1.0]$ is preserved on `chunk.score` for consistent threshold evaluation. |
| Dual Threshold Persistence in `system_configs` | Both `retrieval_threshold` and `unrelated_threshold` are persisted in the database, overriding defaults dynamically on the next student question without server restarts. |
| Interactive Test Sandbox API (`POST /api/admin/retrieval-config/test`) | Allows administrators to run test queries against a target class and view real-time scores, matched document titles, excerpts, and the resulting routing decision. |

## Threshold Decision Boundary

```
      Score >= T_high (0.80)     -->  SOURCE_CURRICULUM (Answer from Class Slides/Docs)
T_low <= Score <  T_high (0.45)  -->  SOURCE_INTERNET   (Search Google/Internet Agent)
         Score <  T_low (0.45)   -->  SOURCE_UNRELATED  (Out of Scope Refusal, No Search)
```

## API Specifications

### `GET /api/admin/retrieval-config` (Admin Only)

Retrieves the currently active upper and lower thresholds.

```json
{
  "threshold": 0.8,
  "default_threshold": 0.8,
  "unrelated_threshold": 0.45,
  "default_unrelated_threshold": 0.45,
  "is_default": true,
  "updated_at": null
}
```

### `PATCH /api/admin/retrieval-config` (Admin Only)

Updates one or both thresholds with validation ($0.0 \le T_{low} \le T_{high} \le 1.0$).

**Request:**
```json
{
  "threshold": 0.75,
  "unrelated_threshold": 0.40
}
```

**Response:** Same shape as `GET` with updated values and `is_default: false`.

### `POST /api/admin/retrieval-config/test` (Admin Only)

Runs a test query against indexed class documents to test threshold sensitivity.

**Request:**
```json
{
  "class_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "query": "What is the time complexity of Dijkstra's algorithm?"
}
```

**Response:**
```json
{
  "top_score": 0.87,
  "decision": "curriculum",
  "threshold": 0.8,
  "unrelated_threshold": 0.45,
  "matched_chunks": [
    {
      "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "document_title": "Graph_Algorithms_Week4.pdf",
      "score": 0.87,
      "snippet": "Dijkstra's algorithm with a min-priority queue runs in O((V + E) log V)..."
    }
  ]
}
```

## Service Contracts

| Component | Function | Description |
|---|---|---|
| `rag_service.py` | `decide_source()` | Evaluates candidate chunks against $T_{high}$ and $T_{low}$, returning `RetrievalDecision`. |
| `rag_service.py` | `get_unrelated_threshold()` | Retrieves persisted lower threshold or falls back to `0.45`. |
| `rag_service.py` | `save_retrieval_threshold()` | Persists both `threshold` and `unrelated_threshold` to `system_configs`. |
| `chat_retrieval_service.py` | `retrieve_chat_chunks()` | Implements early fail-fast bypass before Cross-Encoder transformer model. |
| `reranker_service.py` | `rerank()` | Reranks candidates while preserving original embedding cosine score on `chunk.score`. |

## Acceptance Criteria Status

| AC | Status | Evidence |
|---|---|---|
| AC-01: Retrieval runs first; top score compared with configured threshold | Implemented | `decide_source()` retrieves top vectors before evaluating threshold boundaries. |
| AC-02: `score >= threshold` $\rightarrow$ Curriculum grounded answer | Implemented | Routes to `SOURCE_CURRICULUM` with strict document citations. |
| AC-03: `T_low <= score < threshold` $\rightarrow$ Internet fallback | Implemented | Hands query to Internet agent without throwing errors or creating support tickets. |
| AC-04: `score < T_low` $\rightarrow$ Out of scope refusal | Implemented | Blocks off-topic queries early, preserving LLM tokens and avoiding irrelevant searches. |
| AC-05: Thresholds dynamically configurable by Admin | Implemented | `GET/PATCH /api/admin/retrieval-config` and `POST /api/admin/retrieval-config/test` live in `app/routers/admin/router.py`. |

## Verification Commands

```powershell
# Backend verification
uv run pytest tests/services/rag/ -q
uv run pytest tests/services/chat/test_chat_retrieval.py -q
uv run ruff check .

# Frontend verification
npx vitest run lib/admin-api.test.ts
npm run build
```
