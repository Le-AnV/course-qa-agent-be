"""Tests for Qdrant store wrapper."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document

from app.services.vector_store.qdrant_store import QdrantStore


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
        vector_size=128
    )

    # Check client init
    mock_qdrant_client.assert_called_once_with(url="http://test:6333", api_key="test_key")
    
    # Check collection creation
    mock_client_instance.collection_exists.assert_called_once_with("test_col")
    mock_client_instance.create_collection.assert_called_once()
    
    # Check VectorStore wrapper init
    mock_qdrant_vector_store.assert_called_once_with(
        client=mock_client_instance,
        collection_name="test_col",
        embedding=mock_embeddings
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
def test_qdrant_store_similarity_search_retry(mock_qdrant_client, mock_qdrant_vector_store, mock_sleep):
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
def test_qdrant_store_add_documents_max_retries_exceeded(mock_qdrant_client, mock_qdrant_vector_store, mock_sleep):
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
