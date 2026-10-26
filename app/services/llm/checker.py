"""Check Gemini credentials and configured-model availability without generation."""

from google import genai
from google.genai import errors

from app.config import Settings
from app.schemas.admin import ConnectionTestResult
from app.services.llm.exceptions import LlmConfigurationError, LlmConnectionError


def check_key_connection(api_key: str, model: str) -> ConnectionTestResult:
    """List accessible models to verify one key without consuming generation quota."""

    try:
        with genai.Client(api_key=api_key) as client:
            available_models = {
                candidate.name.rsplit("/", maxsplit=1)[-1]
                for candidate in client.models.list()
                if candidate.name
            }
    except errors.APIError:
        raise LlmConnectionError("Gemini connection failed") from None

    configured_model = model.rsplit("/", maxsplit=1)[-1]
    if configured_model not in available_models:
        raise LlmConnectionError("Configured Gemini model is not available")

    return ConnectionTestResult(
        success=True,
        message="Gemini connection successful",
    )


def check_llm_connection(config: Settings) -> ConnectionTestResult:
    """Check the environment key and configured model."""

    api_key = config.google_api_key
    if api_key is None:
        raise LlmConfigurationError("Gemini API key is required")
    return check_key_connection(api_key.get_secret_value(), config.llm_model)
