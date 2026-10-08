"""Anthropic Claude adapter for `LLMProvider`.

Uses the Messages API with structured outputs (`output_config.format` json_schema), so a
successful response's text block is JSON matching the requested schema. Server-side
refusal fallbacks are enabled (`fallbacks="default"`): if Claude's safety classifiers
decline a request, the API re-runs it on Anthropic's recommended fallback model.
"""

import json
import logging

import anthropic

from app.core.exceptions import LLMContentFilteredError, LLMError, LLMRateLimitError, LLMResponseError, LLMTimeoutError
from app.llm.error_details import provider_error_detail
from app.llm.schemas import LLMRequest, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        api_key: str | None = None,
    ) -> None:
        self._model = model
        # api_key=None lets the SDK resolve credentials from the environment or a profile.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=max_retries
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
                output_config={"format": {"type": "json_schema", "schema": request.output_schema}},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        # APITimeoutError subclasses APIConnectionError, so it must be caught first.
        except anthropic.APITimeoutError as exc:
            raise LLMTimeoutError(f"{request.purpose}: Anthropic request timed out") from exc
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError(
                f"{request.purpose}: the LLM provider is rate-limiting requests (HTTP 429, request "
                f"{exc.request_id}); a provider availability problem, not a task problem"
            ) from exc
        except anthropic.APIStatusError as exc:
            detail = provider_error_detail(exc.body)
            diagnostic = f": {detail}" if detail else ""
            raise LLMError(
                f"{request.purpose}: Anthropic API error {exc.status_code} "
                f"({type(exc).__name__}, request {exc.request_id}){diagnostic}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"{request.purpose}: could not reach the Anthropic API") from exc

        if response.stop_reason == "refusal":
            raise LLMContentFilteredError(
                f"{request.purpose}: the model declined the request (stop_reason='refusal')",
                provider_reason="stop_reason='refusal'",
            )
        if response.stop_reason == "max_tokens":
            raise LLMResponseError(
                f"{request.purpose}: response truncated at max_tokens={request.max_tokens}"
            )

        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise LLMResponseError(f"{request.purpose}: response contained no text block")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"{request.purpose}: response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise LLMResponseError(f"{request.purpose}: response is not a JSON object")

        logger.info(
            "%s: model=%s input_tokens=%s output_tokens=%s request=%s",
            request.purpose,
            response.model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            response._request_id,
        )
        return LLMResponse(
            data=data,
            provider=self.name,
            model=response.model,
            usage=LLMUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )
