"""Tests for Gemini embedding wrapper."""

from unittest.mock import patch

import pytest
from pydantic import SecretStr

from app.config.config import settings
from app.services.embedding.gemini_embedding import GeminiEmbedding

# ---------------------------------------------------------
# REAL API INTEGRATION TESTS
# ---------------------------------------------------------

# Skip these tests if GOOGLE_API_KEY is missing from .env
requires_api_key = pytest.mark.skipif(
    not settings.google_api_key, reason="GOOGLE_API_KEY is not set in .env"
)


@requires_api_key
def test_gemini_embedding_real_api_documents():
    """Test embedding multiple documents with the REAL Gemini API."""
    # Khởi tạo mặc định sẽ lấy key và model từ settings (config.py)
    embedder = GeminiEmbedding()

    texts = ["Hello world", "This is a real test for embedding."]
    vectors = embedder.embed_documents(texts)

    # Kiểm tra kết quả trả về có phải mảng hay không
    assert isinstance(vectors, list)
    assert len(vectors) == 2

    # Kiểm tra chiều dài vector có khớp đúng với cấu hình không
    assert len(vectors[0]) == settings.vector_size
    assert len(vectors[1]) == settings.vector_size


@requires_api_key
def test_gemini_embedding_real_api_query():
    """Test embedding a single query with the REAL Gemini API."""
    embedder = GeminiEmbedding()

    query = "What is the vector dimension?"
    vector = embedder.embed_query(query)

    assert isinstance(vector, list)
    # Kiểm tra chiều dài vector
    assert len(vector) == settings.vector_size


# ---------------------------------------------------------
# MOCK TESTS FOR EDGE CASES (Missing Key, Network Retry)
# ---------------------------------------------------------


@patch("app.services.embedding.gemini_embedding.settings")
def test_gemini_embedding_missing_api_key(mock_settings):
    """Test ValueError is raised when API key is missing."""
    mock_settings.google_api_key = None
    with pytest.raises(ValueError, match="google_api_key is required"):
        GeminiEmbedding()


@patch("app.services.embedding.gemini_embedding.GoogleGenerativeAIEmbeddings")
def test_gemini_embedding_unwraps_secret_api_key(mock_google_embeddings):
    """Pass the configured secret key to the SDK as a plain string."""
    secret_key = SecretStr("test-gemini-key")

    with patch("app.services.embedding.gemini_embedding.settings") as mock_settings:
        mock_settings.google_api_key = secret_key
        mock_settings.embedding_model = "gemini-embedding-2"
        mock_settings.vector_size = 1536

        GeminiEmbedding()

    assert mock_google_embeddings.call_args.kwargs["google_api_key"] == "test-gemini-key"


@patch("app.services.embedding.gemini_embedding.GoogleGenerativeAIEmbeddings")
def test_gemini_embedding_passes_request_options(mock_google_embeddings):
    request_options = {"timeout": 10}

    GeminiEmbedding(api_key="dummy_key", model="dummy_model", request_options=request_options)

    assert mock_google_embeddings.call_args.kwargs["request_options"] == request_options


@patch("time.sleep", return_value=None)  # Skip sleep to speed up tests
@patch("app.services.embedding.gemini_embedding.GoogleGenerativeAIEmbeddings")
def test_embed_documents_retry_success(mock_google_embeddings, mock_sleep):
    """Test retry logic when API fails initially but succeeds later."""
    mock_instance = mock_google_embeddings.return_value
    # 1st call fails, 2nd call succeeds
    mock_instance.embed_documents.side_effect = [Exception("API Rate Limit"), [[0.5]]]

    embedder = GeminiEmbedding(api_key="dummy_key", model="dummy_model")
    result = embedder.embed_documents(["retry_text"])

    assert result == [[0.5]]
    # Ensure it was called exactly twice
    assert mock_instance.embed_documents.call_count == 2


@patch("time.sleep", return_value=None)
@patch("app.services.embedding.gemini_embedding.GoogleGenerativeAIEmbeddings")
def test_embed_query_retry_success(mock_google_embeddings, mock_sleep):
    """Test retry logic for a single query when API fails initially."""
    mock_instance = mock_google_embeddings.return_value
    mock_instance.embed_query.side_effect = [Exception("Network Error"), [0.8]]

    embedder = GeminiEmbedding(api_key="dummy_key", model="dummy_model")
    result = embedder.embed_query("search query")

    assert result == [0.8]
    assert mock_instance.embed_query.call_count == 2


@patch("time.sleep", return_value=None)
@patch("app.services.embedding.gemini_embedding.GoogleGenerativeAIEmbeddings")
def test_embed_documents_max_retries_exceeded(mock_google_embeddings, mock_sleep):
    """Test that it raises an error if it fails continuously."""
    mock_instance = mock_google_embeddings.return_value
    # Always raise an exception
    mock_instance.embed_documents.side_effect = Exception("Persistent API Error")

    embedder = GeminiEmbedding(api_key="dummy_key", model="dummy_model")

    # Should raise tenacity.RetryError eventually
    from tenacity import RetryError

    with pytest.raises(RetryError):
        embedder.embed_documents(["retry_text"])

    # Should try 5 times (according to stop_after_attempt(5) in the code)
    assert mock_instance.embed_documents.call_count == 5


@patch("app.services.embedding.gemini_embedding.GoogleGenerativeAIEmbeddings")
def test_embed_documents_empty_list(mock_google_embeddings):
    """Test behavior when passing an empty list of documents."""
    mock_instance = mock_google_embeddings.return_value
    mock_instance.embed_documents.return_value = []

    embedder = GeminiEmbedding(api_key="dummy_key", model="dummy_model")
    result = embedder.embed_documents([])

    assert result == []
    mock_instance.embed_documents.assert_called_once_with([])
