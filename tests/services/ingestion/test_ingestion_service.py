import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from openpyxl import Workbook

from app.config import settings
from app.services import document_service, ingestion_service


def _record(path, status="processing"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        class_id=uuid.uuid4(),
        file_name="lesson.txt",
        version=1,
        storage_path=str(path),
        status=status,
    )


def test_process_document_indexes_chunks_and_marks_document_indexed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    path = tmp_path / "lesson.txt"
    path.write_text("lesson", encoding="utf-8")
    record = _record(path)
    db = MagicMock()
    chunks = [SimpleNamespace(page_content="lesson text", metadata={})]
    qdrant = MagicMock()

    monkeypatch.setattr(document_service, "get_document", lambda _db, _id: record)
    monkeypatch.setattr(document_service, "build_qdrant_payload", lambda _record: {"file_name": "lesson.txt"})
    monkeypatch.setattr(document_service, "purge_document_vectors", MagicMock())
    def activate(_db, current):
        current.status = "indexed"
        db.commit()

    monkeypatch.setattr(document_service, "supersede_older_versions", activate)
    monkeypatch.setattr(ingestion_service, "_load_document", lambda _path: {"markdown": "# Lesson\nText", "images": {}})
    monkeypatch.setattr(ingestion_service, "QdrantStore", lambda embeddings: qdrant)
    monkeypatch.setattr(ingestion_service, "GeminiEmbedding", lambda: object())
    monkeypatch.setattr(ingestion_service, "markdown_chunker", lambda _text, metadata: chunks)
    monkeypatch.setattr(ingestion_service, "recursive_chunker", lambda docs, metadata: docs)

    ingestion_service.process_document(db, record.id)

    assert record.status == "indexed"
    assert chunks[0].metadata["chunk_index"] == 0
    assert qdrant.add_documents.call_args.args[0] == chunks
    db.commit.assert_called_once()


def test_process_document_indexes_generic_xlsx_with_sheet_row_locator(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    path = tmp_path / "lesson.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Notes"
    sheet.append(["STT", "Nội dung"])
    sheet.append([1, "Dòng 1"])
    workbook.save(path)

    record = _record(path)
    record.file_name = path.name
    db = MagicMock()
    qdrant = MagicMock()

    monkeypatch.setattr(document_service, "get_document", lambda _db, _id: record)
    monkeypatch.setattr(
        document_service,
        "build_qdrant_payload",
        lambda _record: {"file_name": path.name, "file_type": ".xlsx"},
    )
    monkeypatch.setattr(document_service, "purge_document_vectors", MagicMock())

    def mark_indexed(_db, current):
        current.status = "indexed"
        db.commit()

    monkeypatch.setattr(document_service, "supersede_older_versions", mark_indexed)
    monkeypatch.setattr(ingestion_service, "QdrantStore", lambda embeddings: qdrant)
    monkeypatch.setattr(ingestion_service, "GeminiEmbedding", lambda: object())

    ingestion_service.process_document(db, record.id)

    chunks = qdrant.add_documents.call_args.args[0]
    assert record.status == "indexed"
    assert any("Nội dung: Dòng 1" in chunk.page_content for chunk in chunks)
    assert any(chunk.metadata.get("locator") == "Notes - Row 2" for chunk in chunks), [
        chunk.metadata for chunk in chunks
    ]


def test_process_document_marks_failed_and_removes_partial_vectors(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    path = tmp_path / "lesson.txt"
    path.write_text("lesson", encoding="utf-8")
    record = _record(path)
    db = MagicMock()
    qdrant = MagicMock()
    qdrant.add_documents.side_effect = RuntimeError("Qdrant unavailable")
    purge = MagicMock()

    monkeypatch.setattr(document_service, "get_document", lambda _db, _id: record)
    monkeypatch.setattr(document_service, "build_qdrant_payload", lambda _record: {})
    monkeypatch.setattr(document_service, "purge_document_vectors", purge)
    monkeypatch.setattr(ingestion_service, "_load_document", lambda _path: {"markdown": "lesson", "images": {}})
    monkeypatch.setattr(ingestion_service, "QdrantStore", lambda embeddings: qdrant)
    monkeypatch.setattr(ingestion_service, "GeminiEmbedding", lambda: object())
    monkeypatch.setattr(
        ingestion_service,
        "markdown_chunker",
        lambda _text, metadata: [SimpleNamespace(page_content="lesson", metadata={})],
    )
    monkeypatch.setattr(ingestion_service, "recursive_chunker", lambda docs, metadata: docs)

    ingestion_service.process_document(db, record.id)

    assert record.status == "failed"
    assert record.failure_reason == "The vector database is unavailable. Check Qdrant, then retry."
    assert purge.call_count == 2  # clear old points before upsert and partial points after failure
    db.rollback.assert_called_once()
    db.commit.assert_called_once()


def test_failure_reason_explains_embedding_quota():
    error = RuntimeError("429 RESOURCE_EXHAUSTED: embedding quota exceeded")

    assert ingestion_service._failure_reason(error) == (
        "Gemini embedding quota exceeded. Wait for the quota to reset, then retry."
    )


def test_failure_reason_does_not_expose_unknown_value_error_details():
    error = ValueError("Could not read D:/private/course/secret.pdf: parser internals")

    assert ingestion_service._failure_reason(error) == (
        "Document content could not be processed. Check the file and try again."
    )


def test_process_document_ignores_documents_not_processing(monkeypatch, tmp_path):
    record = _record(tmp_path / "lesson.txt", status="pending")
    db = MagicMock()
    load = MagicMock()
    monkeypatch.setattr(document_service, "get_document", lambda _db, _id: record)
    monkeypatch.setattr(ingestion_service, "_load_document", load)

    ingestion_service.process_document(db, record.id)

    load.assert_not_called()
    db.commit.assert_not_called()


def test_process_document_stops_if_document_is_deleted_during_parsing(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    path = tmp_path / "lesson.txt"
    path.write_text("lesson", encoding="utf-8")
    record = _record(path)
    db = MagicMock()
    purge = MagicMock()
    qdrant = MagicMock()

    monkeypatch.setattr(document_service, "get_document", MagicMock(side_effect=[record, None]))
    monkeypatch.setattr(document_service, "build_qdrant_payload", lambda _record: {})
    monkeypatch.setattr(document_service, "purge_document_vectors", purge)
    monkeypatch.setattr(
        ingestion_service,
        "_load_document",
        lambda _path: {"markdown": "lesson", "images": {}},
    )
    monkeypatch.setattr(ingestion_service, "QdrantStore", lambda embeddings: qdrant)
    monkeypatch.setattr(ingestion_service, "GeminiEmbedding", lambda: object())
    monkeypatch.setattr(
        ingestion_service,
        "markdown_chunker",
        lambda _text, metadata: [SimpleNamespace(page_content="lesson", metadata={})],
    )
    monkeypatch.setattr(ingestion_service, "recursive_chunker", lambda docs, metadata: docs)

    ingestion_service.process_document(db, record.id)

    purge.assert_not_called()
    qdrant.add_documents.assert_not_called()


def test_process_document_purges_vectors_if_document_is_deleted_during_upsert(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    path = tmp_path / "lesson.txt"
    path.write_text("lesson", encoding="utf-8")
    record = _record(path)
    db = MagicMock()
    purge = MagicMock()
    qdrant = MagicMock()

    monkeypatch.setattr(document_service, "get_document", MagicMock(side_effect=[record, record, None]))
    monkeypatch.setattr(document_service, "build_qdrant_payload", lambda _record: {})
    monkeypatch.setattr(document_service, "purge_document_vectors", purge)
    monkeypatch.setattr(
        ingestion_service,
        "_load_document",
        lambda _path: {"markdown": "lesson", "images": {}},
    )
    monkeypatch.setattr(ingestion_service, "QdrantStore", lambda embeddings: qdrant)
    monkeypatch.setattr(ingestion_service, "GeminiEmbedding", lambda: object())
    monkeypatch.setattr(
        ingestion_service,
        "markdown_chunker",
        lambda _text, metadata: [SimpleNamespace(page_content="lesson", metadata={})],
    )
    monkeypatch.setattr(ingestion_service, "recursive_chunker", lambda docs, metadata: docs)

    ingestion_service.process_document(db, record.id)

    qdrant.add_documents.assert_called_once()
    assert purge.call_count == 2
    db.commit.assert_not_called()
