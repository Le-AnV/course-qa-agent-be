"""Exceptions for LLM and VLM services."""


class LlmConfigurationError(RuntimeError):
    """Raised when the Gemini API key has not been configured."""


class LlmConnectionError(RuntimeError):
    """Raised when Gemini rejects the request or model configuration."""

