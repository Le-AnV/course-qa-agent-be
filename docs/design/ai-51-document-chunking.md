# AI-51 — Document Chunking & Ingestion Pipeline

## Scope and team decision

This feature covers the core document processing capabilities of the application. It implements the necessary loaders, cleaners, and chunkers to ingest various document formats (PDF, DOCX, Excel) and prepare them for vectorization in the RAG pipeline.

The team decided to separate the document loaders into distinct files within a `loader/` module for better maintainability and single responsibility. The system normalizes all extracted content into Markdown format, enabling semantic chunking (splitting by headers) while retaining critical metadata like page numbers.

## Architecture and Components

| Module/Component | Purpose and use |
|---|---|
| `app/services/ingestion/loader/` | Contains format-specific extractors (`pdf_loader.py`, `docx_loader.py`, `excel_loader.py`, etc.). `pdf_loader.py` marks explicit page markers `<!-- Page number: {page_num} -->` from `pymupdf4llm` metadata and distinguishes empty files from blank whitespace pages. |
| `app/services/ingestion/parser.py` | The main orchestrator. It inspects file extensions, routes the file to the appropriate loader, and handles unsupported file errors. |
| `app/services/ingestion/cleaner.py` | Sanitizes the extracted Markdown. It removes zero-width spaces, normalizes heading spacing (`## Title`), strips emphasis (`**`, `_`, `~~`) from headings while strictly preserving emphasis in body, and protects code blocks, math blocks, and HTML locators. |
| `app/services/ingestion/chunker.py` | Two-phase document-boundary chunker: splits content by `PageSection` (page/slide boundaries) first, then splits by headers (`markdown_chunker`) with parent heading inheritance (`_inherit_headers`), and finally splits by length (`recursive_chunker`). Tracks `section_path`, `page_number`, `slide_number`, and `table_header` in metadata. Preserves raw source text in `page_content` without pollution. Exposes `build_chunk_context(content, metadata)`. |
| `app/services/ingestion/vision.py` | Provides Vision Language Model (VLM) capabilities (via Gemini) to process base64-encoded images/charts extracted from documents and enrich the text with descriptions. |

## Acceptance criteria status

| AC | Status for this task | Notes |
|---|---|---|
| Extract text from PDF, DOCX, and Excel | Implemented | Uses `pymupdf4llm`, `python-docx`, and `openpyxl`. Loaders inject explicit page/slide locators. |
| Document Chunking | Implemented | Page-first and header-aware chunking handled by `chunker.py`. Separators include `["; ", " ", ""]` for long unpunctuated clauses. Tables split by row preserving `table_header` in metadata. Merging chunks across boundaries is forbidden. |
| Markdown Sanitization | Implemented | Headings normalized, protected blocks (code, math, locators) safely restored in `cleaner.py`. |
| Vision Enrichment | Implemented | VLM integration is ready in `vision.py`. |
| Unit Testing | Implemented | Comprehensive test suite added to `tests/services/ingestion/` covering pagination, tables, and locator edge cases. |

## How to verify

The functionality is fully covered by unit tests. You can verify the correctness of loaders, cleaners, and chunkers by running the ingestion test suite:

```bash
uv run pytest tests/ingestion/ -v
```
To check for code style and linting compliance:
```bash
uv run ruff check app/services/ingestion/ tests/ingestion/
```

## Not included in AI-51

- **Database integration:** Storing the generated chunks into Qdrant vector database (this belongs to embedding/retrieval tickets).
- **API Endpoints:** The FastAPI routes to upload files and trigger the ingestion pipeline over HTTP.
- **RAG orchestration:** Passing the chunks into the retrieval context for Gemini.

## Related integration note

The pipeline outputs LangChain `Document` objects. When integrating with the Embedding module, ensure that the chunks' metadata (specifically headers and page numbers) are correctly mapped to the Qdrant payload schema.
