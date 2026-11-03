# AI-53 — Generate Embeddings & Store in Qdrant

## Scope and team decision

This feature implements the core vector embedding generation and vector database storage modules for the Course QA Agent. It enables chunked documents from the ingestion pipeline to be embedded using Google Gemini and indexed into Qdrant for semantic search.

The team decided on the following architectural approaches:
- **Embedding Model & Dimensionality:** Use `gemini-embedding-2` configured with an output dimensionality of `1536` (matching `AI-107` and `settings.vector_size`) via `output_dimensionality` in `langchain-google-genai`.
- **Context vs. Raw Payload Separation:** Vector embeddings are generated using context-enriched representations (`embedding_texts` via `build_chunk_context` containing breadcrumbs and table headers) while the stored payload keeps clean, unpolluted raw `page_content`.
- **Vector Database & Batch Upsert:** Use `Qdrant` via `langchain-qdrant` and `QdrantClient`. Batch uploads into fixed sizes (`DEFAULT_UPSERT_BATCH_SIZE = 64`) to prevent request payload limits.
- **Fault Tolerance & Retries:** Wrap all external network and API calls with exponential backoff retries using `tenacity`. Qdrant upserts isolate retry to `_upsert_points` (avoiding redundant re-embedding calls). Hard per-day quota limit errors from Gemini do not trigger useless retries.
- **RAG Readiness:** Expose the standard LangChain `as_retriever()` interface directly on `QdrantStore` to allow seamless integration into the upcoming RAG generation flow.

## Architecture and Components

| Module/Component | Purpose and use |
|---|---|
| `app/services/embedding/gemini_embedding.py` | Wrapper around `GoogleGenerativeAIEmbeddings`. Implements `embed_documents` and `embed_query` with automatic retry logic (`@retry`), quota error classification (`_always_retry`), and dimensional reduction (`1536`). |
| `app/services/vector_store/qdrant_store.py` | Wrapper around `QdrantVectorStore` and `QdrantClient`. Manages collection lifecycle, document indexing with metadata payloads, support for separate `embedding_texts`, chunked 64-point batch upserts, semantic similarity search, and LangChain retriever export. |
| `app/services/ingestion_service.py` | Generates parallel `embedding_texts` using `build_chunk_context(chunk.page_content, chunk.metadata)` prior to invoking `QdrantStore.add_documents(chunks, embedding_texts=embedding_texts)`. |
| `tests/services/embedding/test_gemini_embedding.py` | Unit and integration tests for Gemini embeddings. Tests real API calls (skipped in CI without API keys), retry on transient failure, max retry limit exceeded, and empty inputs. |
| `tests/services/vector_store/test_qdrant_store.py` | Unit tests for Qdrant store. Tests collection creation vs. existing collection detection, batch upserts, clean payload vs embedding text separation, upsert retry isolation, search retry, and retriever creation. |

## Acceptance criteria status

| AC | Status for this task | Notes |
|---|---|---|
| **AC-01: Call Gemini embedding API (1536 dimensions)** | Implemented | `GeminiEmbedding` invokes `gemini-embedding-2` with `output_dimensionality=settings.vector_size` (1536), aligned with `AI-107`. |
| **AC-02: Qdrant stores vectors with metadata payload** | Implemented | `QdrantStore.add_documents()` upserts LangChain `Document` objects with clean text payloads while allowing `embedding_texts` for context-enriched embeddings. Preserves metadata (`document_id`, `page_number`, `locator`, `section_path`, `table_header`). Batches requests into 64-point chunks. |
| **AC-03: Immediate queryability on Qdrant Server** | Implemented | Qdrant operates as a centralized server; indexed vectors are immediately available for querying via `similarity_search()` without local file syncing. |
| **AC-04: API errors / rate limits retried and logged** | Implemented | Implemented exponential backoff with `tenacity` across all embedding and vector store operations with warning logs on retries. |

## How to verify

Run the test suite for both embedding and vector store modules:

```bash
uv run pytest tests/services/embedding/ tests/services/vector_store/ -v
```

Check code formatting and linting compliance:

```bash
uv run ruff check app/services/embedding/ app/services/vector_store/ tests/services/
```

To run end-to-end integration testing against real Gemini API and running Qdrant instance:
```bash
# Ensure Qdrant is running: sudo docker compose up qdrant -d
unset GEMINI_API_KEY && uv run pytest tests/services/embedding/test_gemini_embedding.py -v
```

## Not included in AI-53

- **Document Parsing and Chunking:** Handled separately in the ingestion pipeline (`AI-51`).
- **HTTP / REST Endpoints:** Upload endpoints and ingestion trigger APIs are deferred to ingestion orchestration tickets.
- **RAG QA Chain:** Combining retrieval with Gemini answer generation and internet fallback search.

## Related integration note

- **Ingestion Pipeline (`AI-51`):** The output of `chunker.py` (`list[Document]`) can be fed directly into `QdrantStore.add_documents(chunks)`.
- **RAG Pipeline:** The future QA service can simply call `store.as_retriever(search_kwargs={"k": settings.retriever_top_k})` to perform retrieval with cosine similarity.

