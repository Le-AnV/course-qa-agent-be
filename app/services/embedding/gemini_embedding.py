"""Gemini embedding service."""

import logging
import re
import time

from langchain_core.embeddings import Embeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pydantic import SecretStr
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config.config import settings

logger = logging.getLogger(__name__)

# Free-tier Gemini counting: every text in a batch counts as one request against
# a 100-requests-per-minute window. Pace document batches so a single upload can
# never exhaust that window: 45 texts every 30s keeps the rolling minute at 90.
DOCUMENT_BATCH_SIZE = 45
BATCH_PACING_SECONDS = 30.0
MAX_RETRY_DELAY_SECONDS = 65.0

_RETRY_DELAY_PATTERNS = (
    re.compile(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'"),
    re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE),
)


def _always_retry(_error: BaseException) -> bool:
    """Retry every provider failure: 429s are per-minute limits, not a dead key."""
    return True


def _retry_delay_seconds(error: BaseException) -> float | None:
    """Return the provider-suggested wait, if the error carries one."""
    message = str(error)
    for pattern in _RETRY_DELAY_PATTERNS:
        match = pattern.search(message)
        if match:
            return min(float(match.group(1)) + 1.0, MAX_RETRY_DELAY_SECONDS)
    return None


def _wait_before_retry(retry_state) -> float:
    """Honour the provider retry hint for rate limits, back off otherwise."""
    outcome = retry_state.outcome
    error = outcome.exception() if outcome is not None else None
    if error is not None:
        suggested = _retry_delay_seconds(error)
        if suggested is not None:
            logger.warning(
                "Gemini embedding rate limited; waiting %.0fs before attempt %s.",
                suggested,
                retry_state.attempt_number + 1,
            )
            return suggested
    return wait_exponential(multiplier=1, min=1, max=10)(retry_state)


class GeminiEmbedding(Embeddings):
    """Wrapper for Google Gemini Embedding API with retry logic."""

    def __init__(self, api_key: str | SecretStr | None = None, model: str | None = None) -> None:
        """Initialize the Gemini embedding wrapper.

        Args:
            api_key (str | SecretStr | None): Google API key. Defaults to settings.google_api_key.
            model (str | None): The embedding model to use. Defaults to settings.embedding_model.
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
        )

    @retry(
        retry=retry_if_exception(_always_retry),
        wait=_wait_before_retry,
        stop=stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            "Retrying Gemini embedding API... Attempt %s. Error: %s",
            retry_state.attempt_number,
            getattr(retry_state.outcome, "exception", lambda: None)() or "unknown error",
        ),
    )
    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        return self._embeddings.embed_documents(texts, batch_size=DOCUMENT_BATCH_SIZE)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents in rate-limited batches with retries (AC-04).

        A single large upload can produce more chunks than the free-tier Gemini
        minute window allows, so batches are paced instead of failing the whole
        document on the first 429.

        Args:
            texts (list[str]): A list of string documents to embed.

        Returns:
            list[list[float]]: A list of embedding vectors.
        """
        if not texts:
            return self._embed_batch([])

        vectors: list[list[float]] = []
        for start in range(0, len(texts), DOCUMENT_BATCH_SIZE):
            if start:
                time.sleep(BATCH_PACING_SECONDS)
            vectors.extend(self._embed_batch(texts[start : start + DOCUMENT_BATCH_SIZE]))
        return vectors

    @retry(
        retry=retry_if_exception(_always_retry),
        wait=_wait_before_retry,
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
