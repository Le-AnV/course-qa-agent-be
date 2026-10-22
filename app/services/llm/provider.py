"""Model provider factory for LLM (RAG/Chat) and VLM (Ingestion/Vision)."""

from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import SecretStr

from app.config import Settings, settings
from app.services.llm.exceptions import LlmConfigurationError


def get_llm_model(
    config: Settings | None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    api_key: str | SecretStr | None = None,
) -> ChatGoogleGenerativeAI:
    """Instantiate and configure Gemini Chat LLM using application settings.

    Args:
        config (Settings | None): Optional application settings instance.
        model (str | None): Optional model override. Defaults to config.llm_model.
        temperature (float | None): Optional temperature override. Defaults to config.llm_temperature.
        api_key (str | SecretStr | None): Optional API key override. Defaults to config.google_api_key.

    Returns:
        ChatGoogleGenerativeAI: The initialized LangChain ChatGoogleGenerativeAI instance for RAG and Chat.
    """
    cfg = config or settings
    resolved_key = api_key or cfg.google_api_key
    if not resolved_key:
        raise LlmConfigurationError("Gemini API key is required")

    key_str = (
        resolved_key.get_secret_value()
        if isinstance(resolved_key, SecretStr)
        else resolved_key
    )
    resolved_model = model or cfg.llm_model
    resolved_temp = temperature if temperature is not None else cfg.llm_temperature

    return ChatGoogleGenerativeAI(
        model=resolved_model,
        google_api_key=key_str,
        temperature=resolved_temp,
    )


def get_vlm_model(
    config: Settings | None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    api_key: str | SecretStr | None = None,
) -> ChatGoogleGenerativeAI:
    """Instantiate and configure Gemini VLM using application settings.

    Args:
        config (Settings | None): Optional application settings instance.
        model (str | None): Optional model override. Defaults to config.vlm_model.
        temperature (float | None): Optional temperature override. Defaults to config.vlm_temperature.
        api_key (str | SecretStr | None): Optional API key override. Defaults to config.google_api_key.

    Returns:
        ChatGoogleGenerativeAI: The initialized LangChain ChatGoogleGenerativeAI instance for vision ingestion.
    """
    cfg = config or settings
    resolved_key = api_key or cfg.google_api_key
    if not resolved_key:
        raise LlmConfigurationError("Gemini API key is required")

    key_str = (
        resolved_key.get_secret_value()
        if isinstance(resolved_key, SecretStr)
        else resolved_key
    )
    resolved_model = model or cfg.vlm_model
    resolved_temp = temperature if temperature is not None else cfg.vlm_temperature

    return ChatGoogleGenerativeAI(
        model=resolved_model,
        google_api_key=key_str,
        temperature=resolved_temp,
    )

