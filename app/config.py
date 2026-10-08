"""Central settings — thresholds and connection strings live here, never hardcoded."""

from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    cors_origins: list[str] = ["http://localhost:3000"]
    database_url: str = "postgresql+psycopg://qa:qa@localhost:5432/course_qa"
    qdrant_url: str = "http://localhost:6333"

    embedding_model: ClassVar[str] = "gemini-embedding-2"
    vector_size: ClassVar[int] = 1536
    retrieval_threshold: ClassVar[float] = 0.8
    content_gap_similarity_threshold: ClassVar[float] = 0.85
    content_gap_search_count: ClassVar[int] = 3
    content_gap_student_percentage: ClassVar[float] = 0.10
    content_gap_window_days: ClassVar[int] = 30

settings = Settings()
