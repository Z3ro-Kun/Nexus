"""The provider interface. NEXUS code depends on this, never on a vendor SDK."""

from typing import Protocol

from app.llm.schemas import LLMRequest, LLMResponse


class LLMProvider(Protocol):
    """Generates one structured (JSON object) response.

    Implementations must bound their own latency and raise `LLMTimeoutError`,
    `LLMResponseError` (unusable output) or `LLMError` (anything else) instead of leaking
    vendor exceptions. They return data only and have no access to NEXUS state.
    """

    name: str

    async def generate(self, request: LLMRequest) -> LLMResponse: ...
