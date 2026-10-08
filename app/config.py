"""Central settings — thresholds and connection strings live here, never hardcoded."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    cors_origins: list[str] = ["http://localhost:3000"]
    database_url: str = "postgresql+psycopg://qa:qa@localhost:5432/course_qa"
    qdrant_url: str = "http://localhost:6333"


settings = Settings()
