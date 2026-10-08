"""Gemini embedding service."""
import logging

from langchain_core.embeddings import Embeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

from app.config.config import get_settings


class GeminiEmbedding(Embeddings):
    """Wrapper for Google Gemini Embedding API with retry logic."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        """Initialize the Gemini embedding wrapper.
        
        Args:
            api_key (str | None): The Google API key. Defaults to settings.google_api_key.
            model (str | None): The embedding model to use. Defaults to settings.embedding_model.
        """
        settings = get_settings()

        api_key = api_key or settings.google_api_key
        model = model or settings.embedding_model

        if not api_key:
            raise ValueError("google_api_key is required for GeminiEmbedding.")

        self._embeddings = GoogleGenerativeAIEmbeddings(
            model=model,
            google_api_key=api_key
        )

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            f"Retrying Gemini embedding API... Attempt {retry_state.attempt_number}. Error: {retry_state.outcome.exception()}"
        )
    )
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of documents with automatic retries on failure (AC-04)."""
        return self._embeddings.embed_documents(texts)

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            f"Retrying Gemini embedding query... Attempt {retry_state.attempt_number}. Error: {retry_state.outcome.exception()}"
        )
    )
    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string with automatic retries on failure (AC-04)."""
        return self._embeddings.embed_query(text)