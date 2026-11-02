"""Gemini embedding service."""

import logging
import re
import time

from langchain_core.embeddings import Embeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pydantic import SecretStr
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config.config import settings
from app.models.gemini_key import GeminiApiKey
from app.services import gemini_key_service

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


def _always_retry(error: BaseException) -> bool:
    """Retry provider failures unless it's a hard per-day quota exhaustion."""
    msg = str(error).lower()
    return not ("per day limit exceeded" in msg or "requests per day" in msg)


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
    """Wrapper for Google Gemini Embedding API with retry and key rotation."""

    def __init__(
        self,
        api_key: str | SecretStr | None = None,
        model: str | None = None,
        db: Session | None = None,
    ) -> None:
        """Initialize the Gemini embedding wrapper.

        Args:
            api_key (str | SecretStr | None): Explicit key for tests/DI.
            model (str | None): The embedding model to use.
            db (Session | None): Session used to select a key from the admin pool.
        """
        self._db = db
        self._model = model or settings.embedding_model
        self._explicit_key = api_key is not None
        self._current_key_id = None
        self._excluded_key_ids: set[object] = set()

        if api_key is None and db is not None:
            resolved = gemini_key_service.resolve_key(db, model=self._model)
            if resolved is not None:
                api_key = resolved.api_key
                self._current_key_id = resolved.key_id

        api_key = api_key or settings.google_api_key
        if not api_key:
            raise ValueError("google_api_key is required for GeminiEmbedding.")

        self._build_client(api_key)

    def _build_client(self, api_key: str | SecretStr) -> None:
        if isinstance(api_key, SecretStr):
            api_key = api_key.get_secret_value()
        self._api_key = api_key
        self._embeddings = GoogleGenerativeAIEmbeddings(
            model=self._model,
            google_api_key=api_key,  # type: ignore
            output_dimensionality=settings.vector_size,
        )

    def _touch(self) -> None:
        if self._db is None or self._current_key_id is None:
            return
        key = self._db.get(GeminiApiKey, self._current_key_id)
        if key is not None:
            gemini_key_service.touch_key(self._db, key)

    def _rotate_key(self, error: BaseException) -> None:
        """Cool down the failed key and rebuild the client with the next one."""

        if self._explicit_key or self._db is None:
            return
        if self._current_key_id is not None:
            current = self._db.get(GeminiApiKey, self._current_key_id)
            if current is not None:
                gemini_key_service.mark_key_failure(self._db, current, error)
                self._excluded_key_ids.add(current.id)
        next_key = gemini_key_service.select_key(
            self._db,
            model=self._model,
            exclude_ids=self._excluded_key_ids,
        )
        if next_key is None:
            return
        self._current_key_id = next_key.id
        self._build_client(gemini_key_service.decrypt_key(next_key))

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
        try:
            vectors = self._embeddings.embed_documents(
                texts, batch_size=DOCUMENT_BATCH_SIZE
            )
        except Exception as error:
            self._rotate_key(error)
            raise
        self._touch()
        return vectors

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

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string, rotating keys without backoff sleeps.

        Query embedding sits on the chat hot path, so a throttled key must be
        swapped immediately instead of waiting for a retry delay. Document
        batches keep their paced retries because uploads are not interactive.

        Args:
            text (str): The query string to embed.

        Returns:
            list[float]: The embedding vector for the query.
        """
        last_error: BaseException | None = None
        for _ in range(3):
            try:
                vector = self._embeddings.embed_query(text)
            except Exception as error:  # noqa: BLE001 - rotate on any provider error
                last_error = error
                self._rotate_key(error)
                continue
            self._touch()
            return vector
        assert last_error is not None
        raise last_error
