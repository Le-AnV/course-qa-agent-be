"""Background document parsing, chunking, embedding, and indexing (AI-55)."""

import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.core.database import get_session_local
from app.services import document_service
from app.services.embedding.gemini_embedding import GeminiEmbedding
from app.services.ingestion.chunker import markdown_chunker, recursive_chunker
from app.services.ingestion.cleaner import clean_marker_markdown
from app.services.vector_store.qdrant_store import QdrantStore

logger = logging.getLogger(__name__)


def _failure_reason(error: Exception) -> str:
    """Return a short message suitable for the trainer-facing UI."""
    message = str(error)
    upper_message = message.upper()
    if "RESOURCE_EXHAUSTED" in upper_message or "QUOTA" in upper_message:
        return "Gemini embedding quota exceeded. Wait for the quota to reset, then retry."
    if isinstance(error, FileNotFoundError):
        return "The stored source file could not be found. Upload the document again."
    if isinstance(error, ValueError):
        return message[:1000]
    if "QDRANT" in upper_message:
        return "The vector database is unavailable. Check Qdrant, then retry."
    return "Document processing failed. Check the backend logs, then retry."


def _load_document(path: str) -> dict:
    # Import lazily because the PDF loader initializes heavyweight models.
    from app.services.ingestion.parser import document_loader

    return document_loader(path)


def process_document(db: Session, document_id: uuid.UUID | str) -> None:
    """Index a processing document and persist indexed/failed status."""
    record = document_service.get_document(db, document_id)
    if record is None or record.status != document_service.STATUS_PROCESSING:
        return

    vector_write_started = False
    try:
        path = Path(record.storage_path)
        if not path.is_file():
            raise FileNotFoundError(f"Stored document is missing: {path}")

        parsed = _load_document(str(path))
        markdown = clean_marker_markdown(parsed.get("markdown", ""))
        if not markdown:
            raise ValueError("No readable text found in document.")

        metadata = document_service.build_qdrant_payload(record)
        chunks = markdown_chunker(markdown, metadata=metadata)
        chunks = recursive_chunker(chunks, metadata=metadata)
        if not chunks:
            raise ValueError("Document did not produce any searchable chunks.")
        for index, chunk in enumerate(chunks):
            chunk.metadata["chunk_index"] = index

        current = document_service.get_document(db, document_id)
        if current is None or current.status != document_service.STATUS_PROCESSING:
            return
        record = current

        # Qdrant's LangChain integration creates point IDs, so clear old
        # points first to make retries idempotent.
        document_service.purge_document_vectors(record.id)
        vector_write_started = True
        QdrantStore(embeddings=GeminiEmbedding()).add_documents(chunks)

        current = document_service.get_document(db, document_id)
        if current is not None and current.status == document_service.STATUS_PROCESSING:
            current.status = document_service.STATUS_INDEXED
            current.failure_reason = None
            db.commit()
    except Exception as error:
        db.rollback()
        if vector_write_started:
            try:
                document_service.purge_document_vectors(document_id)
            except Exception:
                logger.exception("Could not remove partial vectors for document %s", document_id)
        try:
            current = document_service.get_document(db, document_id)
            if current is not None and current.status == document_service.STATUS_PROCESSING:
                current.status = document_service.STATUS_FAILED
                current.failure_reason = _failure_reason(error)
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("Could not mark document %s as failed", document_id)
        logger.exception("Document ingestion failed for %s", document_id)


def process_document_in_background(document_id: str) -> None:
    """Run ingestion with its own DB session after the HTTP response."""
    session_factory = get_session_local(settings.database_url)
    with session_factory() as db:
        process_document(db, document_id)
