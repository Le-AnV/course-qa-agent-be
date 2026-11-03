"""Tests for course relevance gate and document validation service."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import document_service, document_validation_service, ingestion_service
from app.services.document_validation_service import (
    _parse_validation_json,
    validate_document_relevance,
)


def test_parse_validation_json_clean():
    payload = '{"is_relevant": true, "confidence": 0.95, "reason": "Matches syllabus"}'
    result = _parse_validation_json(payload)
    assert result.is_relevant is True
    assert result.confidence == 0.95
    assert result.reason == "Matches syllabus"


def test_parse_validation_json_markdown_fenced():
    payload = """```json
    {
      "is_relevant": false,
      "confidence": 0.92,
      "reason": "English grammar in C++ OOP course"
    }
    ```"""
    result = _parse_validation_json(payload)
    assert result.is_relevant is False
    assert result.confidence == 0.92
    assert "English grammar" in result.reason


def test_parse_validation_json_malformed():
    result = _parse_validation_json("not valid json at all")
    assert result.is_relevant is True
    assert result.confidence == 0.0


def test_validate_document_relevance_empty_text():
    db = MagicMock()
    is_relevant, reason = validate_document_relevance(
        db,
        class_name="C++ Programming",
        class_code="CPP101",
        class_description="Learn C++",
        file_name="empty.txt",
        document_text="   ",
    )
    assert is_relevant is True
    assert reason == "Empty document excerpt"


def test_validate_document_relevance_relevant(monkeypatch):
    db = MagicMock()
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.text = '{"is_relevant": true, "confidence": 0.95, "reason": "Core C++ pointers lecture"}'
    fake_client.models.generate_content.return_value = fake_response

    monkeypatch.setattr(
        document_validation_service.gemini_key_service,
        "resolve_key",
        lambda _db, **kwargs: SimpleNamespace(api_key="fake-key", key=None, key_id=None),
    )
    monkeypatch.setattr(document_validation_service.genai, "Client", lambda api_key: fake_client)

    is_relevant, reason = validate_document_relevance(
        db,
        class_name="C++ Programming",
        class_code="CPP101",
        class_description="Object-Oriented Programming in C++",
        file_name="pointers.pdf",
        document_text="Chapter 4: Pointers and Memory Management in C++",
    )

    assert is_relevant is True
    assert "Core C++" in reason


def test_validate_document_relevance_confident_rejection(monkeypatch):
    db = MagicMock()
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.text = '{"is_relevant": false, "confidence": 0.93, "reason": "Document covers English grammar and TOEFL prep"}'
    fake_client.models.generate_content.return_value = fake_response

    monkeypatch.setattr(
        document_validation_service.gemini_key_service,
        "resolve_key",
        lambda _db, **kwargs: SimpleNamespace(api_key="fake-key", key=None, key_id=None),
    )
    monkeypatch.setattr(document_validation_service.genai, "Client", lambda api_key: fake_client)

    is_relevant, reason = validate_document_relevance(
        db,
        class_name="C++ Programming",
        class_code="CPP101",
        class_description="Object-Oriented Programming in C++",
        file_name="english_grammar.pdf",
        document_text="Lesson 1: Present Perfect Tense and Irregular Verbs",
    )

    assert is_relevant is False
    assert "English grammar" in reason


def test_validate_document_relevance_low_confidence_fails_open(monkeypatch):
    db = MagicMock()
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.text = '{"is_relevant": false, "confidence": 0.65, "reason": "General math formulas, might be prerequisite"}'
    fake_client.models.generate_content.return_value = fake_response

    monkeypatch.setattr(
        document_validation_service.gemini_key_service,
        "resolve_key",
        lambda _db, **kwargs: SimpleNamespace(api_key="fake-key", key=None, key_id=None),
    )
    monkeypatch.setattr(document_validation_service.genai, "Client", lambda api_key: fake_client)

    is_relevant, _ = validate_document_relevance(
        db,
        class_name="C++ Programming",
        class_code="CPP101",
        class_description="Object-Oriented Programming in C++",
        file_name="math_notes.pdf",
        document_text="Linear algebra basics for 3D graphics",
    )

    # Low confidence (< 0.8) must fail open to avoid false rejections
    assert is_relevant is True


def test_validate_document_relevance_api_error_fails_open(monkeypatch):
    db = MagicMock()
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = RuntimeError("API network timeout")

    monkeypatch.setattr(
        document_validation_service.gemini_key_service,
        "resolve_key",
        lambda _db, **kwargs: SimpleNamespace(api_key="fake-key", key=None, key_id=None),
    )
    monkeypatch.setattr(document_validation_service.genai, "Client", lambda api_key: fake_client)

    is_relevant, reason = validate_document_relevance(
        db,
        class_name="C++ Programming",
        class_code="CPP101",
        class_description="Object-Oriented Programming in C++",
        file_name="course_notes.pdf",
        document_text="Some document content here",
    )

    assert is_relevant is True
    assert "error" in reason.lower()


def test_ingestion_service_rejects_irrelevant_document(monkeypatch, tmp_path):
    doc_path = tmp_path / "english.pdf"
    doc_path.write_text("English grammar rules", encoding="utf-8")

    record = SimpleNamespace(
        id=uuid.uuid4(),
        class_id=uuid.uuid4(),
        status="processing",
        file_name="english.pdf",
        file_type="pdf",
        failure_reason=None,
    )

    fake_class = SimpleNamespace(
        name="C++ Programming",
        code="CPP101",
        info=SimpleNamespace(description="OOP in C++"),
    )

    db = MagicMock()
    db.get.return_value = fake_class

    monkeypatch.setattr(document_service, "get_document", lambda _db, _id: record)
    monkeypatch.setattr(document_service, "document_path", lambda _rec: doc_path)
    monkeypatch.setattr(ingestion_service, "_load_document", lambda _p: {"markdown": "English grammar rules"})
    monkeypatch.setattr(
        document_validation_service,
        "validate_document_relevance",
        lambda db, **kwargs: (False, "Content focuses entirely on English grammar."),
    )

    purge_mock = MagicMock()
    monkeypatch.setattr(document_service, "purge_document_vectors", purge_mock)

    ingestion_service.process_document(db, record.id)

    assert record.status == "failed"
    assert "English grammar" in record.failure_reason
    assert "English grammar" in record.failure_reason
    purge_mock.assert_not_called()


def test_ingestion_service_force_bypasses_relevance_check(monkeypatch, tmp_path):
    doc_path = tmp_path / "english.pdf"
    doc_path.write_text("English grammar rules", encoding="utf-8")

    record = SimpleNamespace(
        id=uuid.uuid4(),
        class_id=uuid.uuid4(),
        status="processing",
        file_name="english.pdf",
        file_type="pdf",
        failure_reason=None,
    )

    fake_class = SimpleNamespace(
        name="C++ Programming",
        code="CPP101",
        info=SimpleNamespace(description="OOP in C++"),
    )

    db = MagicMock()
    db.get.return_value = fake_class

    monkeypatch.setattr(document_service, "get_document", lambda _db, _id: record)
    monkeypatch.setattr(document_service, "document_path", lambda _rec: doc_path)
    monkeypatch.setattr(ingestion_service, "_load_document", lambda _p: {"markdown": "English grammar rules"})
    validate_mock = MagicMock(return_value=(False, "Content focuses entirely on English grammar."))
    monkeypatch.setattr(document_validation_service, "validate_document_relevance", validate_mock)
    monkeypatch.setattr(ingestion_service, "recursive_chunker", lambda text, **kwargs: [SimpleNamespace(page_content="chunk 1", metadata={})])
    monkeypatch.setattr(ingestion_service, "build_chunk_context", lambda text, chunks: ["ctx 1"])
    fake_embedding = MagicMock()
    fake_embedding.embed_chunks.return_value = [[0.1, 0.2]]
    monkeypatch.setattr(ingestion_service, "GeminiEmbedding", lambda **kwargs: fake_embedding)
    monkeypatch.setattr(ingestion_service, "QdrantStore", MagicMock())
    monkeypatch.setattr(document_service, "purge_document_vectors", MagicMock())
    monkeypatch.setattr(document_service, "supersede_older_versions", MagicMock())

    # When force=True, validate_document_relevance must NOT be called
    ingestion_service.process_document(db, record.id, force=True)

    validate_mock.assert_not_called()
    assert record.status != "failed"
