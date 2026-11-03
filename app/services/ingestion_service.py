"""Background document parsing, chunking, embedding, and indexing (AI-55)."""

import logging
import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.core.database import get_session_local
from app.models.course import Class
from app.services import document_service, document_validation_service
from app.services.document_validation_service import DocumentRelevanceError
from app.services.embedding.gemini_embedding import GeminiEmbedding
from app.services.ingestion.chunker import build_chunk_context, markdown_chunker, recursive_chunker
from app.services.ingestion.cleaner import clean_marker_markdown
from app.services.ingestion.syllabus_parser import (
    SyllabusFormatError,
    is_syllabus_document,
    parse_syllabus_markdown,
)
from app.services.vector_store.qdrant_store import QdrantStore

logger = logging.getLogger(__name__)


def _failure_reason(error: Exception) -> str:
    """Return a short message suitable for the trainer-facing UI."""
    message = str(error)
    upper_message = message.upper()
    if "RESOURCE_EXHAUSTED" in upper_message or "QUOTA" in upper_message:
        if "PER DAY" in upper_message or "PERDAY" in upper_message:
            return "Gemini embedding quota is exhausted for today. Retry tomorrow."
        return "Gemini embedding rate limit was hit repeatedly. Wait a minute, then retry the document."
    if isinstance(error, FileNotFoundError):
        return "The stored source file could not be found. Upload the document again."
    if isinstance(error, DocumentRelevanceError):
        return f"Course relevance: {error}"
    if isinstance(error, SyllabusFormatError):
        return "Syllabus format is not recognized or contains no course units."
    if isinstance(error, ValueError):
        if message in {
            "No readable text found in document.",
            "Document did not produce any searchable chunks.",
            "Syllabus format is not recognized or contains no course units.",
        }:
            return message
        return "Document content could not be processed. Check the file and try again."
    if "QDRANT" in upper_message:
        return "The vector database is unavailable. Check Qdrant, then retry."
    return "Document processing failed. Check the backend logs, then retry."


def _load_document(path: str) -> dict:
    # Import lazily because the PDF loader initializes heavyweight models.
    from app.services.ingestion.parser import document_loader

    return document_loader(path, clean=False)


def process_document(db: Session, document_id: uuid.UUID | str, force: bool = False) -> None:
    """Index a processing document and persist indexed/failed status."""
    record = document_service.get_document(db, document_id)
    if record is None or record.status != document_service.STATUS_PROCESSING:
        return

    vector_write_started = False
    try:
        path = document_service.document_path(record)
        if not path.is_file():
            raise FileNotFoundError(f"Stored document is missing: {path}")

        parsed = _load_document(str(path))
        markdown = clean_marker_markdown(parsed.get("markdown", ""))
        if not markdown:
            raise ValueError("No readable text found in document.")

        file_name = getattr(record, "file_name", "") or path.name

        # Validate course relevance before vector indexing
        clazz = db.get(Class, record.class_id)
        if (
            clazz
            and isinstance(getattr(clazz, "name", None), str)
            and not force
        ):
            class_desc = clazz.info.description if getattr(clazz, "info", None) else None
            is_relevant, rejection_reason = document_validation_service.validate_document_relevance(
                db,
                class_name=clazz.name,
                class_code=clazz.code,
                class_description=class_desc,
                file_name=file_name,
                document_text=markdown,
            )
            if not is_relevant:
                raise DocumentRelevanceError(rejection_reason or "Document does not match course subject.")

        roadmap = (
            parse_syllabus_markdown(markdown)
            if is_syllabus_document(file_name, markdown)
            else None
        )

        metadata = document_service.build_qdrant_payload(record)
        chunks = markdown_chunker(markdown, metadata=metadata)
        chunks = recursive_chunker(chunks)
        if not chunks:
            raise ValueError("Document did not produce any searchable chunks.")

        embedding_texts = []
        for index, chunk in enumerate(chunks):
            chunk.metadata["chunk_index"] = index
            embedding_texts.append(build_chunk_context(chunk.page_content, chunk.metadata))

        current = document_service.get_document(db, document_id)
        if current is None or current.status != document_service.STATUS_PROCESSING:
            return
        record = current

        # Qdrant's LangChain integration creates point IDs, so clear old
        # points first to make retries idempotent.
        document_service.purge_document_vectors(record.id)
        vector_write_started = True
        QdrantStore(embeddings=GeminiEmbedding(db=db)).add_documents(
            chunks, embedding_texts=embedding_texts
        )

        current = document_service.get_document(db, document_id)
        if current is None or current.status != document_service.STATUS_PROCESSING:
            try:
                document_service.purge_document_vectors(document_id)
            except Exception:
                logger.exception("Could not remove vectors for cancelled document %s", document_id)
            return
        document_service.supersede_older_versions(db, current)
        if roadmap is not None and getattr(current, "status", None) == document_service.STATUS_INDEXED:
            document_service.replace_syllabus_roadmap(db, current, roadmap)
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


def process_document_in_background(document_id: str, force: bool = True) -> None:
    """Run ingestion with its own DB session after the HTTP response."""
    session_factory = get_session_local(settings.database_url)
    with session_factory() as db:
        process_document(db, document_id, force=force)
