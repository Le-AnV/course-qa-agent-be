# AI-107 — Embedding Model and Retrieval Thresholds

## Scope and team decision

The original Jira story describes settings editable from an Admin dashboard. The
team decided that these values will be fixed in backend code for now. To change a
value, update `app/config.py`, rebuild/redeploy the backend Docker image, and
re-embed data if the embedding model or vector size changes. Dashboard editing and
database-backed configuration are outside this task's current scope.

> **AI-65 update:** `retrieval_threshold` is now the exception. AI-65 (AC-05 /
> T35) stores the active value in `system_config` and exposes it through
> `GET/PATCH /api/admin/retrieval-config`, so an admin can change it without a
> redeploy. `settings.retrieval_threshold = 0.8` remains the documented default
> used when no valid DB value exists. See
> `docs/design/ai-65-retrieval-score-threshold.md`. The other values below stay
> fixed in code.

The Jira default of two students for a content gap is outdated. The team's current
decision is to require at least 10% of active students in the class.

## Fixed configuration

| Setting | Value | Purpose and use |
|---|---:|---|
| `embedding_model` | `gemini-embedding-2` | Model identifier for generating document/query vectors. Future ingestion and RAG services must use this value. |
| `vector_size` | `1536` | Required vector dimension for embeddings stored in Qdrant. It must match the embedding output and collection configuration. |
| `retrieval_threshold` | `0.8` | Minimum knowledge-base similarity score for selecting a KB answer instead of Internet fallback. The future RAG flow must read this setting. |
| `content_gap_similarity_threshold` | `0.85` | Similarity threshold for grouping similar questions into a content-gap topic. |
| `content_gap_search_count` | `3` | Minimum Internet searches for a topic in the configured period. |
| `content_gap_student_percentage` | `0.10` (10%) | Minimum share of active students in a class asking about the topic. The eventual count is `ceil(active_students × 0.10)`. |
| `content_gap_window_days` | `30` days | Period in which searches and distinct students are counted for a content-gap topic. |

These are `ClassVar` values in `app/config.py`, so environment variables do not
override them. The current backend skeleton does not yet contain the ingestion,
RAG, or content-gap processing flows; those later services must consume these
values when implemented.

## How to change and verify a value

1. Edit the corresponding `ClassVar` in `app/config.py`.
2. Run the configuration test and lint check:

   ```powershell
   uv run python -m pytest tests/embedding/ -q
   uv run ruff check .
   ```

3. Rebuild and redeploy the backend Docker image so the running application loads
   the changed code.
4. If `embedding_model` or `vector_size` changed, rebuild the Qdrant collection
   and re-embed its data. Existing vectors must not be queried with incompatible
   dimensions or a different embedding model.

The configuration test verifies all confirmed fixed values. It does not call
Gemini or Qdrant and does not test a real question/retrieval flow.

## Acceptance criteria status

| AC | Status for this task | Notes |
|---|---|---|
| AC-01: embedding model and vector size | Implemented as fixed code configuration, per team decision | Dashboard selection is deferred. Changing model/vector size requires a Qdrant collection rebuild and re-embedding. |
| AC-02: retrieval threshold, default `0.8` | Implemented as a fixed code value | The future RAG implementation must consume it when comparing KB scores. |
| AC-03: content-gap thresholds | Confirmed values are implemented in code | The 10% active-student rule supersedes Jira's old two-student default. Counting/grouping behavior belongs to the future content-gap implementation. |
| AC-04: a configuration change applies to the next question | Deployment behavior documented; end-to-end use deferred | Values are fixed in code and require a backend rebuild/redeploy. The current skeleton has no question route, so the future RAG service must read the values; this task does not claim live-question verification. |

## Not included in AI-107

- Admin API or dashboard editing of these values.
- Chat endpoint, Qdrant retrieval, Gemini answer generation, or Internet fallback.
- Content-gap aggregation/counting logic.
- Automatic Qdrant collection rebuild or re-embedding.
- End-to-end question testing; the RAG work is scheduled for a later sprint.

## Verification

AI-107 configuration test:

```text
uv run python -m pytest tests/embedding/ -q -> 1 passed
```

The full local working tree previously passed 31 tests and Ruff, but it also
contains uncommitted AI-105 work. The AI-107 MR pipeline should be used to verify
the committed branch independently. A real question flow is not available in the
current backend skeleton.

## Related integration note

The separate `features/Implementation_EmbeddingQdrant` branch was previously
documented with a Qdrant vector size of `3072`. Align that collection size with
AI-107's `1536` before integrating the two branches.
