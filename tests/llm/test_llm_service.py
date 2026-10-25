"""Tests for Gemini API-key and model availability checks."""

import pytest
from google.genai import errors

from app.config import Settings
from app.services.llm import (
    LlmConfigurationError,
    LlmConnectionError,
    check_llm_connection,
    get_llm_model,
    get_vlm_model,
)


class FakeModel:
    """Represent the model name returned by the Gemini SDK."""

    def __init__(self, name):
        """Create a minimal SDK model record for connection-check tests."""
        self.name = name


class FakeGeminiClient:
    """Expose a minimal context-managed Gemini client for unit tests."""

    def __init__(self, model_names):
        """Create a fake Gemini client exposing the requested model names."""
        self.models = self
        self.model_names = model_names
        self.closed = False

    def __enter__(self):
        """Return this fake client when the service opens its context manager."""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Record that the fake client was closed after the check."""
        self.closed = True
        return False

    def list(self):
        """Return fake SDK model records for the configured model names."""
        return [FakeModel(name) for name in self.model_names]


def test_llm_connection_lists_models_without_generating_content(monkeypatch):
    """Verify the configured model is accessible without a generation call."""
    clients = []

    def create_client(*, api_key):
        """Return a fake client after checking the SDK receives the key value."""
        assert api_key == "gemini-secret"
        client = FakeGeminiClient(["models/gemini-3.5-flash-lite"])
        clients.append(client)
        return client

    monkeypatch.setattr("app.services.llm.checker.genai.Client", create_client)
    config = Settings(_env_file=None, google_api_key="gemini-secret")

    result = check_llm_connection(config)

    assert result.success is True
    assert result.message == "Gemini connection successful"
    assert clients[0].closed is True


def test_llm_connection_rejects_missing_api_key(monkeypatch):
    """Reject a Gemini connection check when no member key is configured."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    config = Settings(_env_file=None, google_api_key=None)

    with pytest.raises(LlmConfigurationError):
        check_llm_connection(config)


def test_get_llm_model_success():
    """Ensure get_llm_model instantiates ChatGoogleGenerativeAI with expected config."""
    config = Settings(_env_file=None, google_api_key="test-key")
    llm = get_llm_model(config)

    assert llm.model == config.llm_model
    assert llm.temperature == config.llm_temperature
    assert llm.google_api_key.get_secret_value() == "test-key"


def test_get_vlm_model_success():
    """Ensure get_vlm_model instantiates ChatGoogleGenerativeAI with expected VLM config."""
    config = Settings(_env_file=None, google_api_key="test-key")
    vlm = get_vlm_model(config)

    assert vlm.model == config.vlm_model
    assert vlm.temperature == config.vlm_temperature
    assert vlm.google_api_key.get_secret_value() == "test-key"


def test_get_llm_model_rejects_missing_api_key(monkeypatch):
    """Reject get_llm_model when no API key is configured."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    config = Settings(_env_file=None, google_api_key=None)

    with pytest.raises(LlmConfigurationError, match="Gemini API key is required"):
        get_llm_model(config)


def test_llm_connection_rejects_unavailable_configured_model(monkeypatch):
    """Reject a key that cannot access the model fixed in application config."""
    monkeypatch.setattr(
        "app.services.llm.checker.genai.Client",
        lambda **kwargs: FakeGeminiClient(["models/another-model"]),
    )
    config = Settings(_env_file=None, google_api_key="gemini-secret")

    with pytest.raises(LlmConnectionError) as error:
        check_llm_connection(config)

    assert str(error.value) == "Configured Gemini model is not available"
    assert "gemini-secret" not in str(error.value)


def test_llm_connection_hides_provider_error_details(monkeypatch):
    """Hide provider error details that could contain credentials from clients."""

    class FailingGeminiClient(FakeGeminiClient):
        def list(self):
            """Simulate a provider error that includes sensitive key text."""
            raise errors.ClientError(
                401,
                {"message": "invalid API key: gemini-secret"},
            )

    monkeypatch.setattr(
        "app.services.llm.checker.genai.Client",
        lambda **kwargs: FailingGeminiClient([]),
    )
    config = Settings(_env_file=None, google_api_key="gemini-secret")

    with pytest.raises(LlmConnectionError) as error:
        check_llm_connection(config)

    assert str(error.value) == "Gemini connection failed"
    assert "gemini-secret" not in str(error.value)
