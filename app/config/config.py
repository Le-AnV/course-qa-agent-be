"""Central settings — thresholds and connection strings live here, never hardcoded."""

from pydantic_settings import BaseSettings, SettingsConfigDict

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized configuration management for the Mini-RAG application.

    Uses pydantic-settings to validate environment variables, parse default values,
    and provide typed application configuration across all modules.

    Attributes:
        qdrant_url: Base URL of the Qdrant vector database server.
        qdrant_api_key: Optional API key for authenticating with Qdrant Cloud or secured instances.
        collection_name: Name of the Qdrant collection storing document embeddings.
        embedding_model: HuggingFace model identifier for generating dense embeddings.
        embedding_device: Hardware accelerator device for embeddings ('cpu', 'cuda', or None for auto).
        llm_model: Model identifier for Google Gemini generative AI.
        llm_temperature: Temperature setting for LLM responses (0.0 for deterministic answers).
        google_api_key: API key for Google Gemini model access.
        chunk_size: Maximum character count per document chunk.
        chunk_overlap: Overlap character count between consecutive chunks.
        separators: List of string separators used for recursive character splitting.
        headers_to_split_on: List of tuples specifying markdown headers to split on.
        retriever_top_k: Default number of relevant chunks retrieved per query.
        prompts_dir: Directory path containing modular markdown prompt templates.
        log_level: Logging verbosity level (DEBUG, INFO, WARNING, ERROR).
    """
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    cors_origins: list[str] = ["http://localhost:3000"]
    database_url: str = "postgresql+psycopg://qa:qa@localhost:5432/course_qa"
    qdrant_url: str = "http://localhost:6333"

    qdrant_api_key: Optional[str] = Field(default=None, validation_alias="QDRANT_API_KEY")
    collection_name: str = Field(default="assistant-documents-agent", validation_alias="COLLECTION_NAME")

    embedding_model: str = Field(default="models/gemini-embedding-2", validation_alias="EMBEDDING_MODEL")
    embedding_device: Optional[str] = Field(default=None, validation_alias="EMBEDDING_DEVICE")

    llm_model: str = Field(default="models/gemini-3.8-flash", validation_alias="LLM_MODEL")
    llm_temperature: float = Field(default=1.0, validation_alias="LLM_TEMPERATURE")

    vlm_model: str = Field(default="models/gemini-2.5-flash", validation_alias="VLM_MODEL")
    vlm_temperature: float = Field(default=1.0, validation_alias="VLM_TEMPERATURE")

    google_api_key: Optional[str] = Field(default=None, validation_alias="GOOGLE_API_KEY")

    chunk_size: int = Field(default=800, validation_alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=120, validation_alias="CHUNK_OVERLAP")
    separators: list[str] = Field(
        default=["\n\n", "\n", ". ", "? ", "! ", " ", ""], 
        validation_alias="SEPARATORS"
    )
    headers_to_split_on: list[tuple[str, str]] = Field(
        default=[
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
            ("####", "Header 4")
        ],
        validation_alias="HEADERS_TO_SPLIT_ON"
    )
    retriever_top_k: int = Field(default=3, validation_alias="RETRIEVER_TOP_K")

    # Save prompt templates in backend/app/prompt.yaml file
    prompt_file: Path = Field(
        default=Path(__file__).resolve().parent / "prompt.yaml",
        validation_alias="PROMPT_FILE",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retrieve and cache the singleton Settings instance.

    Returns:
        Settings: Validated runtime configuration settings.
    """
    return Settings()
