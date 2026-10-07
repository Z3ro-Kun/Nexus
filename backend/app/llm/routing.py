"""Role-based LLM routing: different roles can use different providers and models.

    agent / planner / replanner / verifier  -> LLMRequest (purpose, metadata)
        -> RoutingProvider: role_for(request) -> the configured LLMProvider for that role
        -> existing adapter (OpenAIProvider for Gemini / OpenRouter, or the default provider)

The route is decided by deterministic configuration (NEXUS_<ROLE>_PROVIDER / _MODEL),
never by a model. The role comes only from fields NEXUS code sets on the request: the
`purpose` ("planner", "replanner", "verifier", "agent:<type>") and, for conflict
resolution, the `conflict_id` metadata the agent runtime adds from the task's own state.
Model output never reaches either, and metadata is never sent to a provider. Agents stay
provider-agnostic: they still call one `LLMProvider.generate`.

Unconfigured roles use the default provider (NEXUS_LLM_PROVIDER), so a deployment
without any role settings behaves exactly as before (build_provider then does not even
create a router).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.config import HOSTED_PROVIDERS, LLM_ROLES, Settings
from app.core.exceptions import LLMConfigurationError, LLMError
from app.llm.base import LLMProvider
from app.llm.schemas import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

DIRECT_PURPOSES = {"planner": "planner", "replanner": "replanner", "verifier": "verifier"}
AGENT_ROLES = frozenset({"researcher", "analyst", "specialist"})


def role_for(request: LLMRequest) -> str | None:
    """The routing role of a request, from code-set fields only. None: not a routed role
    (e.g. a connectivity ping); it goes to the default provider."""
    if request.purpose in DIRECT_PURPOSES:
        return DIRECT_PURPOSES[request.purpose]
    if request.purpose.startswith("agent:"):
        agent_type = request.purpose.removeprefix("agent:")
        if agent_type == "researcher" and request.metadata.get("conflict_id"):
            return "conflict_resolver"
        if agent_type in AGENT_ROLES:
            return agent_type
    return None


@dataclass(frozen=True)
class ResolvedLLMConfig:
    """Where a role's requests go. Holds no secret (the key is read only when the
    adapter is built), so it is safe to log or display."""

    role: str
    provider: str  # "default" | "gemini" | "openrouter" | "groq"
    model: str | None  # None with "default": NEXUS_LLM_MODEL
    base_url: str | None


def resolve_llm_config(settings: Settings, role: str) -> ResolvedLLMConfig:
    if role not in LLM_ROLES:
        raise LLMConfigurationError(f"unknown LLM role {role!r}")
    provider = getattr(settings, f"{role}_provider") or "default"
    model = getattr(settings, f"{role}_model")
    base_url = getattr(settings, f"{provider}_base_url") if provider in HOSTED_PROVIDERS else None
    return ResolvedLLMConfig(role=role, provider=provider, model=model, base_url=base_url)


def configured_routes(settings: Settings) -> dict[str, ResolvedLLMConfig]:
    """Roles with an explicit route (others use the default provider)."""
    return {
        role: resolve_llm_config(settings, role) for role in LLM_ROLES if getattr(settings, f"{role}_provider") is not None
    }


class RoutingProvider:
    """An LLMProvider that sends each request to its role's provider."""

    name = "router"

    def __init__(
        self,
        routes: Mapping[str, LLMProvider],
        *,
        labels: Mapping[str, str] | None = None,
        default: LLMProvider | None = None,
    ) -> None:
        self._routes = dict(routes)
        self._labels = dict(labels or {})
        self._default = default

    def provider_for(self, request: LLMRequest) -> tuple[str | None, LLMProvider | None]:
        role = role_for(request)
        return role, self._routes.get(role or "", self._default)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        role, provider = self.provider_for(request)
        if provider is None:
            raise LLMError(f"{request.purpose}: no LLM provider is configured for role {role or 'default'!r}")
        response = await provider.generate(request)
        label = self._labels.get(role or "")
        # Record which provider actually answered (e.g. "gemini"), not the adapter type.
        return response.model_copy(update={"provider": label}) if label else response


class UnavailableProvider:
    """Stands in for a route that cannot be served (missing API key, or no default
    provider). Fails every request clearly, before anything is sent. The app still
    starts, so a missing key for one role does not break unrelated use."""

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMConfigurationError(f"{request.purpose}: {self.reason}")


def routing_problems(settings: Settings) -> list[str]:
    """Why configured routes cannot be served (names settings, never values)."""
    problems = []
    for role, config in configured_routes(settings).items():
        if config.provider == "default" and settings.llm_provider == "none":
            problems.append(f"NEXUS_{role.upper()}_PROVIDER=default, but no default provider is configured (NEXUS_LLM_PROVIDER)")
        elif config.provider in HOSTED_PROVIDERS and getattr(settings, f"{config.provider}_api_key") is None:
            problems.append(f"role {role} is routed to {config.provider}, but NEXUS_{config.provider.upper()}_API_KEY is not set")
    return problems


def build_routing_provider(settings: Settings, default: LLMProvider | None) -> LLMProvider | None:
    """The default provider if no role is routed; else a RoutingProvider. A route that
    cannot be served (its provider's API key is missing, or "default" without a default
    provider) gets an UnavailableProvider: its requests fail with LLMConfigurationError
    (no secrets in the message), and a warning names the missing setting at startup."""
    routes = configured_routes(settings)
    if not routes:
        return default
    from app.llm.openai_provider import OpenAIProvider

    built: dict[tuple[str, str | None], LLMProvider] = {}
    providers: dict[str, LLMProvider] = {}
    labels: dict[str, str] = {}
    for role, config in routes.items():
        if config.provider == "default":
            providers[role] = default if default is not None else UnavailableProvider(
                f"NEXUS_{role.upper()}_PROVIDER=default, but no default provider is configured (NEXUS_LLM_PROVIDER)"
            )
            continue
        key = getattr(settings, f"{config.provider}_api_key")
        if key is None:
            reason = f"role {role} is routed to {config.provider}, but NEXUS_{config.provider.upper()}_API_KEY is not set"
            logger.warning("LLM routing: %s; its requests will fail", reason)
            providers[role] = UnavailableProvider(reason)
            labels[role] = config.provider
            continue
        cache_key = (config.provider, config.model)
        if cache_key not in built:
            built[cache_key] = OpenAIProvider(
                model=config.model or "",
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                api_key=key.get_secret_value(),
                base_url=config.base_url,
                response_format=settings.openai_response_format,
                strict_schema=settings.openai_strict_schema,
                max_tokens_param=settings.openai_max_tokens_param,
            )
        providers[role] = built[cache_key]
        labels[role] = config.provider
    return RoutingProvider(providers, labels=labels, default=default)
