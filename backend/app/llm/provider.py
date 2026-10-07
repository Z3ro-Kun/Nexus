"""Builds the configured provider. The only place that knows which adapters exist."""

from app.core.config import Settings
from app.llm.base import LLMProvider


def build_provider(settings: Settings) -> LLMProvider | None:
    """Return the configured provider, or None when LLM features are disabled. With
    role routes configured (NEXUS_<ROLE>_PROVIDER), a RoutingProvider over them, with
    this provider as the default for unrouted roles (app.llm.routing)."""
    from app.llm.routing import build_routing_provider

    return build_routing_provider(settings, _build_default(settings))


def _build_default(settings: Settings) -> LLMProvider | None:
    if settings.llm_provider == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider

        api_key = settings.anthropic_api_key
        return AnthropicProvider(
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            api_key=api_key.get_secret_value() if api_key is not None else None,
        )
    if settings.llm_provider == "openai":
        from app.llm.openai_provider import OpenAIProvider

        openai_key = settings.openai_api_key
        return OpenAIProvider(
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            api_key=openai_key.get_secret_value() if openai_key is not None else None,
            base_url=settings.openai_base_url,
            response_format=settings.openai_response_format,
            strict_schema=settings.openai_strict_schema,
            max_tokens_param=settings.openai_max_tokens_param,
        )
    return None
