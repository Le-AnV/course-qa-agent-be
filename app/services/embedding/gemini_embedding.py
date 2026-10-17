"""Gemini embedding service."""

import logging

from langchain_core.embeddings import Embeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pydantic import SecretStr
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config.config import settings

logger = logging.getLogger(__name__)


class GeminiEmbedding(Embeddings):
    """Wrapper for Google Gemini Embedding API with retry logic."""

    def __init__(
        self,
        api_key: str | SecretStr | None = None,
        model: str | None = None,
        request_options: dict | None = None,
    ) -> None:
        """Initialize the Gemini embedding wrapper.

        Args:
            api_key (str | SecretStr | None): Google API key. Defaults to settings.google_api_key.
            model (str | None): The embedding model to use. Defaults to settings.embedding_model.
            request_options (dict | None): Optional request options passed to the Google API client.
        """
        api_key = api_key or settings.google_api_key
        model = model or settings.embedding_model

        if not api_key:
            raise ValueError("google_api_key is required for GeminiEmbedding.")

        if isinstance(api_key, SecretStr):
            api_key = api_key.get_secret_value()

        self._embeddings = GoogleGenerativeAIEmbeddings(
            model=model,
            google_api_key=api_key,  # type: ignore
            output_dimensionality=settings.vector_size,
            request_options=request_options,
        )

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            "Retrying Gemini embedding API... Attempt %s. Error: %s",
            retry_state.attempt_number,
            getattr(retry_state.outcome, "exception", lambda: None)() or "unknown error",
        ),
    )
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of documents with automatic retries on failure (AC-04).

        Args:
            texts (list[str]): A list of string documents to embed.

        Returns:
            list[list[float]]: A list of embedding vectors.
        """
        return self._embeddings.embed_documents(texts)

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            "Retrying Gemini embedding query... Attempt %s. Error: %s",
            retry_state.attempt_number,
            getattr(retry_state.outcome, "exception", lambda: None)() or "unknown error",
        ),
    )
    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string with automatic retries on failure (AC-04).

        Args:
            text (str): The query text to embed.

        Returns:
            list[float]: The embedding vector for the query.
        """
        return self._embeddings.embed_query(text)
