"""Central settings — thresholds and connection strings live here, never hardcoded."""

from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import AliasChoices, EmailStr, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized configuration management for the Course QA application.

    Uses pydantic-settings to validate environment variables, parse default values,
    and provide typed application configuration across all modules.

    Attributes:
        app_env: Environment mode ('local', 'development', 'production').
        cors_origins: List of allowed CORS origins.
        database_url: PostgreSQL connection string.
        qdrant_url: Base URL of the Qdrant vector database server.
        qdrant_api_key: Optional API key for authenticating with Qdrant.
        collection_name: Name of the Qdrant collection storing document embeddings.
        vector_size: Dimension size of vector embeddings (constant).
        retrieval_threshold: Similarity score threshold for RAG retrieval.
        content_gap_similarity_threshold: Threshold for clustering content gap queries.
        content_gap_search_count: Minimum searches needed to flag a content gap.
        content_gap_student_percentage: Percentage of students querying before gap is flagged.
        content_gap_window_days: Time window in days for content gap analysis.
        embedding_model: Model identifier for dense embeddings.
        embedding_device: Hardware accelerator device for embeddings ('cpu', 'cuda', or None for auto).
        llm_model: Model identifier for Google Gemini generative AI.
        llm_temperature: Temperature setting for LLM responses.
        vlm_model: Model identifier for Vision Language Model.
        vlm_temperature: Temperature setting for VLM responses.
        google_api_key: Secret API key for Google Gemini model access.
        jwt_secret: HS256 signing key for DB-backed login tokens (dev default, override in prod).
        jwt_algorithm: JWT algorithm for login tokens.
        jwt_expire_minutes: Login token lifetime in minutes.
        bootstrap_admin_email/password: First admin seeded on a fresh DB.
        smtp_sender_email: Optional shared sender email address.
        smtp_app_password: Optional Gmail App Password for SMTP authentication.
        email_dry_run: Record AI-31 outbox mail without real SMTP.
        tavily_api_key: API key for Tavily search provider.
        chunk_size: Maximum character count per document chunk.
        chunk_overlap: Overlap character count between consecutive chunks.
        separators: List of string separators used for recursive character splitting.
        headers_to_split_on: List of tuples specifying markdown headers to split on.
        retriever_top_k: Default number of relevant chunks retrieved per query.
        chat_context_exchanges: Number of previous Q&A turns used for follow-up questions.
        chat_context_max_chars: Maximum characters of conversation history sent to the LLM.
        prompt_file: File path or directory pointing to prompt templates.
        log_level: Logging verbosity level (DEBUG, INFO, WARNING, ERROR).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3100",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3100",
    ]
    # Optional regex for extra dev origins; local defaults to any localhost port.
    cors_origin_regex: str | None = None
    database_url: str = "postgresql+psycopg://qa:qa@localhost:5432/course_qa"

    # Vector store configuration
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = Field(default=None, validation_alias="QDRANT_API_KEY")
    collection_name: str = Field(
        default="assistant-documents-agent", validation_alias="COLLECTION_NAME"
    )
    vector_size: ClassVar[int] = 1536
    retrieval_threshold: ClassVar[float] = 0.8
    content_gap_similarity_threshold: ClassVar[float] = 0.85
    content_gap_search_count: ClassVar[int] = 3
    content_gap_student_percentage: ClassVar[float] = 0.10
    content_gap_window_days: ClassVar[int] = 30

    # Model configuration
    embedding_model: str = Field(default="gemini-embedding-2", validation_alias="EMBEDDING_MODEL")
    embedding_device: str | None = Field(default=None, validation_alias="EMBEDDING_DEVICE")
    reranker_model: str = Field(
        default="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        validation_alias="RERANKER_MODEL",
    )

    llm_model: ClassVar[str] = "models/gemini-3.5-flash-lite"
    llm_temperature: float = Field(default=1.0, validation_alias="LLM_TEMPERATURE")

    vlm_model: str = Field(default="models/gemini-3.5-flash", validation_alias="VLM_MODEL")
    vlm_temperature: float = Field(default=1.0, validation_alias="VLM_TEMPERATURE")

    google_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    )
    tavily_api_key: str | None = Field(default=None, validation_alias="TAVILY_API_KEY")

    # DB-backed auth (E1 / AI-25, AI-27, AI-31). Dev defaults only — override in prod.
    jwt_secret: str = Field(
        default="dev-only-secret-change-me-use-env-in-prod",
        validation_alias="JWT_SECRET",
    )
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    jwt_expire_minutes: int = Field(default=60 * 12, validation_alias="JWT_EXPIRE_MINUTES")
    # Bootstrap admin so a fresh DB is usable before any admin exists.
    # Canonical seed/bootstrap admin credential shared with INITIAL_USERS in
    # app/core/seed.py so a freshly seeded DB always has a usable admin.
    bootstrap_admin_email: str = Field(
        default="admin@academic.edu.vn", validation_alias="BOOTSTRAP_ADMIN_EMAIL"
    )
    bootstrap_admin_password: str = Field(
        default="Admin@123", validation_alias="BOOTSTRAP_ADMIN_PASSWORD"
    )

    # Shared outbound email configuration for Admin test messages and mail services.
    smtp_host: ClassVar[str] = "smtp.gmail.com"
    smtp_port: ClassVar[int] = 587
    smtp_security: ClassVar[str] = "STARTTLS"
    smtp_sender_email: EmailStr | None = Field(
        default=None,
        validation_alias=AliasChoices("SMTP_SENDER_EMAIL", "SMTP_SENDER"),
    )
    smtp_app_password: SecretStr | None = None
    email_rate_limit_per_minute: ClassVar[int] = 20
    # AI-31 outbox: True records mail without real SMTP (local dev / tests).
    email_dry_run: bool = Field(default=True, validation_alias="EMAIL_DRY_RUN")

    # Persistent directory for uploaded source files used by the ingestion pipeline.
    document_storage_dir: Path = Field(
        default=Path(__file__).resolve().parents[2] / "storage" / "documents",
        validation_alias="DOCUMENT_STORAGE_DIR",
    )

    @property
    def smtp_sender(self) -> str:
        """AI-31 compat: plain sender string (canonical source: smtp_sender_email)."""

        return str(self.smtp_sender_email or "")

    @property
    def smtp_rate_per_minute(self) -> int:
        """AI-31 compat: alias of the code-fixed email_rate_limit_per_minute."""

        return self.email_rate_limit_per_minute

    # Chunking and retrieval configuration
    chunk_size: int = Field(default=800, validation_alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=120, validation_alias="CHUNK_OVERLAP")
    separators: list[str] = Field(
        default=["\n\n", "\n", ". ", "? ", "! "],
        validation_alias="SEPARATORS",
    )
    headers_to_split_on: list[tuple[str, str]] = Field(
        default=[
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
            ("####", "Header 4"),
        ],
        validation_alias="HEADERS_TO_SPLIT_ON",
    )
    retriever_top_k: int = Field(default=3, validation_alias="RETRIEVER_TOP_K")

    # AI-63 conversation memory: keep the last N complete Q&A exchanges.
    chat_context_exchanges: int = Field(
        default=5,
        ge=1,
        le=20,
        validation_alias="CHAT_CONTEXT_EXCHANGES",
    )
    chat_context_max_chars: int = Field(
        default=6000,
        ge=500,
        le=30000,
        validation_alias="CHAT_CONTEXT_MAX_CHARS",
    )

    # Prompt configuration
    prompt_file: Path = Field(
        default=Path(__file__).resolve().parent / "prompts",
        validation_alias="PROMPT_FILE",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator(
        "google_api_key",
        "smtp_sender_email",
        "smtp_app_password",
        mode="before",
    )
    @classmethod
    def normalize_blank_optional_values(cls, value):
        """Treat blank optional environment variables as unconfigured values."""

        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retrieve and cache the singleton Settings instance.

    Returns:
        Settings: Validated runtime configuration settings.
    """
    return Settings()


settings = get_settings()
