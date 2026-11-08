"""Phase 1 unit tests for Content Gap query embedding capture and logging (AI-91, AC-01 to AC-03).

Covers:
- Vector store collection creation for 'content_gap_queries'
- Query embedding sync to Qdrant without question text (privacy compliance)
- Correct payload fields (class_id, student_id, timestamp, source, fallback_reason, concept_tag, embedding_model_version)
- Point deletion by message ID on session cleanup
- QdrantStore.similarity_search_by_vector_with_score
"""

import uuid
from unittest.mock import MagicMock

from qdrant_client.http import models as qmodels

from app.services import history_service
from app.services.vector_store.qdrant_store import QdrantStore


def test_ensure_content_gap_collection_creates_when_missing():
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = False

    history_service.ensure_content_gap_collection(mock_client)

    mock_client.collection_exists.assert_called_once_with(
        history_service.CONTENT_GAP_COLLECTION
    )
    mock_client.create_collection.assert_called_once()
    _, kwargs = mock_client.create_collection.call_args
    assert kwargs["collection_name"] == history_service.CONTENT_GAP_COLLECTION
    vectors_config = kwargs["vectors_config"]
    assert vectors_config.size == 1536
    assert vectors_config.distance == qmodels.Distance.COSINE


def test_ensure_content_gap_collection_skips_when_exists():
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True

    history_service.ensure_content_gap_collection(mock_client)

    mock_client.collection_exists.assert_called_once_with(
        history_service.CONTENT_GAP_COLLECTION
    )
    mock_client.create_collection.assert_not_called()


def test_sync_content_gap_query_stores_vector_without_question_text():
    mock_client = MagicMock()
    dummy_vector = [0.1] * 1536
    msg_id = str(uuid.uuid4())
    class_id = str(uuid.uuid4())
    student_id = str(uuid.uuid4())

    history_service.sync_content_gap_query(
        message_id=msg_id,
        class_id=class_id,
        student_id=student_id,
        embedding=dummy_vector,
        source="web",
        fallback_reason="similarity_below_threshold",
        concept_tag="binary search",
        embedding_model_version="text-embedding-004",
        qdrant_client=mock_client,
    )

    mock_client.upsert.assert_called_once()
    call_kwargs = mock_client.upsert.call_args.kwargs
    assert call_kwargs["collection_name"] == history_service.CONTENT_GAP_COLLECTION
    points = call_kwargs["points"]
    assert len(points) == 1

    point = points[0]
    assert point.id == msg_id
    assert point.vector == dummy_vector

    payload = point.payload
    # AC-01: payload must contain the required fields
    assert payload["class_id"] == class_id
    assert payload["student_id"] == student_id
    assert payload["source"] == "web"
    assert payload["fallback_reason"] == "similarity_below_threshold"
    assert payload["concept_tag"] == "binary search"
    assert payload["embedding_model_version"] == "text-embedding-004"
    assert "timestamp" in payload

    # Strict Privacy Check: AC-01 prohibits storing question / query text
    assert "question" not in payload
    assert "query" not in payload
    assert "prompt" not in payload
    assert "text" not in payload
    assert "content" not in payload


def test_sync_content_gap_query_handles_empty_embedding_gracefully():
    mock_client = MagicMock()
    # None or empty embedding should not call upsert
    history_service.sync_content_gap_query(
        message_id=str(uuid.uuid4()),
        class_id=str(uuid.uuid4()),
        student_id=str(uuid.uuid4()),
        embedding=[],
        qdrant_client=mock_client,
    )
    mock_client.upsert.assert_not_called()


def test_sync_content_gap_query_exception_does_not_raise():
    mock_client = MagicMock()
    mock_client.upsert.side_effect = RuntimeError("Qdrant unreachable")

    # Should log warning but not raise error to break user chat turn
    history_service.sync_content_gap_query(
        message_id=str(uuid.uuid4()),
        class_id=str(uuid.uuid4()),
        student_id=str(uuid.uuid4()),
        embedding=[0.2] * 1536,
        source="material",
        qdrant_client=mock_client,
    )


def test_delete_content_gap_queries():
    mock_client = MagicMock()
    message_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    history_service.delete_content_gap_queries(
        message_ids=message_ids,
        qdrant_client=mock_client,
    )

    mock_client.delete.assert_called_once()
    call_kwargs = mock_client.delete.call_args.kwargs
    assert call_kwargs["collection_name"] == history_service.CONTENT_GAP_COLLECTION
    points_selector = call_kwargs["points_selector"]
    assert points_selector.points == message_ids


def test_qdrant_store_similarity_search_by_vector_with_score():
    mock_vector_store = MagicMock(spec=["similarity_search_with_score_by_vector"])
    mock_doc = MagicMock()
    mock_doc.page_content = "Introduction to Algorithms"
    mock_doc.metadata = {"document_id": "doc-1", "page": 1}
    mock_vector_store.similarity_search_with_score_by_vector.return_value = [
        (mock_doc, 0.88)
    ]

    store = object.__new__(QdrantStore)
    store.vector_store = mock_vector_store

    vector = [0.05] * 1536
    results = store.similarity_search_by_vector_with_score(
        embedding=vector,
        k=3,
        class_id="class-xyz",
    )

    assert len(results) == 1
    doc, score = results[0]
    assert score == 0.88
    assert doc.page_content == "Introduction to Algorithms"
    assert doc.metadata["document_id"] == "doc-1"

    mock_vector_store.similarity_search_with_score_by_vector.assert_called_once()
    _, kwargs = mock_vector_store.similarity_search_with_score_by_vector.call_args
    assert kwargs["k"] == 3
    qfilter = kwargs["filter"]
    assert qfilter is not None
    assert qfilter.must[0].key == "metadata.class_id"
    assert qfilter.must[0].match.value == "class-xyz"
