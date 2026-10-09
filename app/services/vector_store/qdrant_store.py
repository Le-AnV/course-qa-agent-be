"""Qdrant vector store service."""

import logging

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config.config import settings

logger = logging.getLogger(__name__)


class QdrantStore:
    """Wrapper for Qdrant vector store with retry logic."""

    def __init__(
        self,
        embeddings: Embeddings,
        url: str | None = None,
        collection_name: str | None = None,
        api_key: str | None = None,
        vector_size: int | None = None,
    ) -> None:
        """Initialize the Qdrant store wrapper.

        Args:
            embeddings (Embeddings): The embedding model instance (e.g., GeminiEmbedding).
            url (str | None): Qdrant server URL. Defaults to settings.qdrant_url.
            collection_name (str | None): Name of the collection. Defaults to settings.collection_name.
            api_key (str | None): Qdrant API key (if needed). Defaults to settings.qdrant_api_key.
            vector_size (int | None): Dimension of the embeddings. Defaults to settings.vector_size.
        """
        url = url or settings.qdrant_url
        collection_name = collection_name or settings.collection_name
        api_key = api_key or settings.qdrant_api_key
        vector_size = vector_size or settings.vector_size

        if not url or not collection_name:
            raise ValueError("qdrant_url and collection_name must be configured.")

        self.client = QdrantClient(url=url, api_key=api_key)
        self.collection_name = collection_name
        self.embeddings = embeddings

        # Ensure collection exists before initializing QdrantVectorStore
        self._ensure_collection(vector_size)

        self.vector_store = QdrantVectorStore(
            client=self.client,
            collection_name=self.collection_name,
            embedding=self.embeddings,
        )

    def _ensure_collection(self, vector_size: int) -> None:
        """Create the collection if it does not exist.
        
        Args:
            vector_size (int): The dimension size of the vectors.
        """
        if not self.client.collection_exists(self.collection_name):
            logger.info("Creating Qdrant collection '%s' with size %d", self.collection_name, vector_size)
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            "Retrying Qdrant add_documents... Attempt %s. Error: %s",
            retry_state.attempt_number,
            getattr(retry_state.outcome, "exception", lambda: None)() or "unknown error",
        )
    )
    def add_documents(self, documents: list[Document]) -> list[str]:
        """Upsert documents into Qdrant.

        Metadata from Document objects are automatically stored as payload (AC-02).
        Data is queryable immediately upon successful upsert (AC-03).

        Args:
            documents (list[Document]): List of Langchain Document objects to upsert.

        Returns:
            list[str]: A list of generated vector IDs.
        """
        return self.vector_store.add_documents(documents)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            "Retrying Qdrant similarity search... Attempt %s. Error: %s",
            retry_state.attempt_number,
            getattr(retry_state.outcome, "exception", lambda: None)() or "unknown error",
        )
    )
    def similarity_search(self, query: str, k: int | None = None) -> list[Document]:
        """Perform semantic search on the vector store.

        Args:
            query (str): The text query to search for.
            k (int | None): Number of documents to return. Defaults to settings.retriever_top_k.

        Returns:
            list[Document]: The top k most similar documents from the vector store.
        """
        k = k or settings.retriever_top_k
        return self.vector_store.similarity_search(query, k=k)