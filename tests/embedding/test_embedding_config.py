"""Tests for the fixed embedding and retrieval configuration."""

from app.config import Settings


def test_settings_use_confirmed_embedding_defaults():
    settings = Settings(_env_file=None)

    assert settings.embedding_model == "gemini-embedding-2"
    assert settings.vector_size == 1536
    assert settings.retrieval_threshold == 0.8
    assert settings.content_gap_similarity_threshold == 0.85
    assert settings.content_gap_search_count == 3
    assert settings.content_gap_student_percentage == 0.10
    assert settings.content_gap_window_days == 30
