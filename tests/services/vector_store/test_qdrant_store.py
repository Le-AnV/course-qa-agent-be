"""Tests for Qdrant store wrapper."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from app.services.vector_store.qdrant_store import QdrantStore, delete_points_by_document_id


def _embedding_retry_error():
    from tenacity import Retr  yError, retry, stop_after_attempt, wait_none

    @retry(stop=stop_after_attempt(1), wait=wait_none())
    def fail():
        raise RuntimeError("embedding failed")

    try:
        fail()
    except RetryError as error:
        return error
    raise AssertionError("Expected retry error")


@patch("app.services.vector_store.qdrant_store.QdrantVectorStore")
@patch("app.services.vector_store.qdrant_store.QdrantClient")
def test_qdrant_store_initialization(mock_qdrant_client, mock_qdrant_vector_store):
    """Test QdrantStore correctly initializes and creates collection if missing."""
    mock_client_instance = mock_qdrant_client.return_value
    mock_client_instance.collection_exists.return_value = False

    mock_embeddings = MagicMock()

    QdrantStore(
        embeddings=mock_embeddings,
        url="http://test:6333",
        collection_name="test_col",
        api_key="test_key",
        vector_size=128,
    )

    # Check client init
    mock_qdrant_client.assert_called_once_with(url="http://test:6333", api_key="test_key")

    # Check collection creation
    mock_client_instance.collection_exists.assert_called_once_with("test_col")
    mock_client_instance.create_collection.assert_called_once()

    # Check VectorStore wrapper init
    mock_qdrant_vector_store.assert_called_once_with(
        client=mock_client_instance, collection_name="test_col", embedding=mock_embeddings
    )


@patch("time.sleep", return_value=None)
@patch("app.services.vector_store.qdrant_store.QdrantVectorStore")
@patch("app.services.vector_store.qdrant_store.QdrantClient")
def test_qdrant_store_add_documents_retry(mock_qdrant_client, mock_qdrant_vector_store, mock_sleep):
    """Test add_documents retries on intermittent failure (AC-04)."""
    mock_client_instance = mock_qdrant_client.return_value
    mock_client_instance.collection_exists.return_value = True

    mock_vector_store_instance = mock_qdrant_vector_store.return_value
    # Fail first, succeed second
    mock_vector_store_instance.add_documents.side_effect = [Exception("Network Error"), ["id1"]]

    store = QdrantStore(embeddings=MagicMock(), url="dummy", collection_name="dummy")

    docs = [Document(page_content="test", metadata={"page": 1})]
    result = store.add_documents(docs)

    assert result == ["id1"]
    assert mock_vector_store_instance.add_documents.call_count == 2


@patch("time.sleep", return_value=None)
@patch("app.services.vector_store.qdrant_store.QdrantVectorStore")
@patch("app.services.vector_store.qdrant_store.QdrantClient")
def test_qdrant_store_does_not_retry_embedding_retry_error(
    mock_qdrant_client, mock_qdrant_vector_store, mock_sleep
):
    """Qdrant must not multiply retries already exhausted by the embedder."""
    mock_qdrant_client.return_value.collection_exists.return_value = True
    mock_store = mock_qdrant_vector_store.return_value
    mock_store.add_documents.side_effect = _embedding_retry_error()
    store = QdrantStore(embeddings=MagicMock(), url="dummy", collection_name="dummy")

    with pytest.raises(Exception, match="RetryError"):
        store.add_documents([Document(page_content="lesson")])

    assert mock_store.add_documents.call_count == 1


@patch("time.sleep", return_value=None)
@patch("app.services.vector_store.qdrant_store.QdrantVectorStore")
@patch("app.services.vector_store.qdrant_store.QdrantClient")
def test_qdrant_store_does_not_retry_embedding_quota_error(
    mock_qdrant_client, mock_qdrant_vector_store, mock_sleep
):
    mock_qdrant_client.return_value.collection_exists.return_value = True
    mock_store = mock_qdrant_vector_store.return_value
    mock_store.add_documents.side_effect = Exception("429 RESOURCE_EXHAUSTED: quota exceeded")
    store = QdrantStore(embeddings=MagicMock(), url="dummy", collection_name="dummy")

    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        store.add_documents([Document(page_content="lesson")])

    assert mock_store.add_documents.call_count == 1


@patch("time.sleep", return_value=None)
@patch("app.services.vector_store.qdrant_store.QdrantVectorStore")
@patch("app.services.vector_store.qdrant_store.QdrantClient")
def test_qdrant_store_similarity_search_retry(
    mock_qdrant_client, mock_qdrant_vector_store, mock_sleep
):
    """Test similarity_search retries on intermittent failure."""
    mock_client_instance = mock_qdrant_client.return_value
    mock_client_instance.collection_exists.return_value = True

    mock_vector_store_instance = mock_qdrant_vector_store.return_value
    doc_result = [Document(page_content="result")]
    mock_vector_store_instance.similarity_search.side_effect = [Exception("Timeout"), doc_result]

    store = QdrantStore(embeddings=MagicMock(), url="dummy", collection_name="dummy")

    result = store.similarity_search("query", k=1)

    assert result == doc_result
    assert mock_vector_store_instance.similarity_search.call_count == 2


@patch("app.services.vector_store.qdrant_store.QdrantVectorStore")
@patch("app.services.vector_store.qdrant_store.QdrantClient")
def test_qdrant_store_collection_exists(mock_qdrant_client, mock_qdrant_vector_store):
    """Test initialization when collection already exists (should not create it)."""
    mock_client_instance = mock_qdrant_client.return_value
    mock_client_instance.collection_exists.return_value = True

    QdrantStore(embeddings=MagicMock(), url="dummy", collection_name="existing_col")

    # Should check if collection exists
    mock_client_instance.collection_exists.assert_called_once_with("existing_col")
    # Should NOT attempt to create the collection
    mock_client_instance.create_collection.assert_not_called()


@patch("time.sleep", return_value=None)
@patch("app.services.vector_store.qdrant_store.QdrantVectorStore")
@patch("app.services.vector_store.qdrant_store.QdrantClient")
def test_qdrant_store_add_documents_max_retries_exceeded(
    mock_qdrant_client, mock_qdrant_vector_store, mock_sleep
):
    """Test add_documents raises RetryError if it fails continuously."""
    mock_client_instance = mock_qdrant_client.return_value
    mock_client_instance.collection_exists.return_value = True

    mock_vector_store_instance = mock_qdrant_vector_store.return_value
    mock_vector_store_instance.add_documents.side_effect = Exception("Persistent DB Error")

    store = QdrantStore(embeddings=MagicMock(), url="dummy", collection_name="dummy")

    from tenacity import RetryError

    with pytest.raises(RetryError):
        store.add_documents([Document(page_content="test")])

    # Should try 5 times based on retry logic configuration
    assert mock_vector_store_instance.add_documents.call_count == 5


@patch("app.services.vector_store.qdrant_store.QdrantVectorStore")
@patch("app.services.vector_store.qdrant_store.QdrantClient")
def test_qdrant_store_as_retriever(mock_qdrant_client, mock_qdrant_vector_store):
    """Test getting the retriever from the store correctly applies arguments."""
    mock_client_instance = mock_qdrant_client.return_value
    mock_client_instance.collection_exists.return_value = True

    mock_vector_store_instance = mock_qdrant_vector_store.return_value
    mock_retriever = MagicMock()
    mock_vector_store_instance.as_retriever.return_value = mock_retriever

    store = QdrantStore(embeddings=MagicMock(), url="dummy", collection_name="dummy")

    retriever = store.as_retriever(search_kwargs={"k": 5})

    # Should call internal as_retriever with right arguments and return it
    mock_vector_store_instance.as_retriever.assert_called_once_with(search_kwargs={"k": 5})
    assert retriever == mock_retriever


def test_delete_points_by_document_id_purges_matching_chunks():
    """AI-39 (AC-01): one filter deletes every chunk of the file."""
    client = MagicMock()
    client.collection_exists.return_value = True
    client.count.return_value = MagicMock(count=3)

    removed = delete_points_by_document_id(
        "doc-1",
        url="http://test:6333",
        collection_name="test_col",
        client=client,
    )

    assert removed == 3
    assert client.count.call_args.kwargs["collection_name"] == "test_col"
    selector = client.delete.call_args.kwargs["points_selector"]
    assert selector.must[0].key == "metadata.document_id"
    assert selector.must[0].match.value == "doc-1"
    assert client.delete.call_args.kwargs["wait"] is True


def test_delete_points_by_document_id_skips_missing_collection():
    """Nothing was ever indexed: report 0 without touching the server."""
    client = MagicMock()
    client.collection_exists.return_value = False

    removed = delete_points_by_document_id(
        "doc-1",
        url="http://test:6333",
        collection_name="test_col",
        client=client,
    )

    assert removed == 0
    client.delete.assert_not_called()


def test_delete_points_by_document_id_against_real_qdrant_semantics():
    """AI-39 (AC-01): the payload filter removes exactly one file's points.

    Runs against qdrant-client's in-memory server implementation, so the
    Filter/MatchValuecod selector is exercised for real (no Docker).
    """
    from qdrant_client import QdrantClient
    from qdrant_client.http.models import Distance, PointStruct, VectorParams

    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="test_col",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    client.upsert(
        collection_name="test_col",
        points=[
            PointStruct(
                id=1,
                vector=[0.1, 0.2],
                payload={"metadata": {"document_id": "doc-1"}},
            ),
            PointStruct(
                id=2,
                vector=[0.3, 0.4],
                payload={"metadata": {"document_id": "doc-1"}},
            ),
            PointStruct(
                id=3,
                vector=[0.5, 0.6],
                payload={"metadata": {"document_id": "doc-2"}},
            ),
        ],
    )

    removed = delete_points_by_document_id(
        "doc-1",
        url="http://unused",
        collection_name="test_col",
        client=client,
    )

    assert removed == 2
    # Only the other document's chunk survives (AC-02: no answers from doc-1).
    remaining = client.count(collection_name="test_col", exact=True).count
    assert remaining == 1
    assert [p.payload["metadata"]["document_id"] for p in client.scroll("test_col")[0]] == ["doc-2"]
