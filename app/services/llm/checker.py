"""Check Gemini credentials and configured-model availability without generation."""

from google import genai
from google.genai import errors

from app.config import Settings
from app.schemas.admin import ConnectionTestResult
from app.services.llm.exceptions import LlmConfigurationError, LlmConnectionError


def check_llm_connection(config: Settings) -> ConnectionTestResult:
    """List accessible models to verify the key without consuming generation quota."""

    api_key = config.google_api_key
    if api_key is None:
        raise LlmConfigurationError("Gemini API key is required")

    try:
        with genai.Client(api_key=api_key.get_secret_value()) as client:
            available_models = {
                model.name.rsplit("/", maxsplit=1)[-1]
                for model in client.models.list()
                if model.name
            }
    except errors.APIError:
        raise LlmConnectionError("Gemini connection failed") from None

    configured_model = config.llm_model.rsplit("/", maxsplit=1)[-1]
    if configured_model not in available_models:
        raise LlmConnectionError("Configured Gemini model is not available")

    return ConnectionTestResult(
        success=True,
        message="Gemini connection successful",
    )

