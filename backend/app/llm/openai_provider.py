"""OpenAI-compatible adapter for `LLMProvider`.

Uses the Chat Completions API (the one every OpenAI-compatible server implements) via the
OpenAI SDK with a configurable `base_url`, so it works against OpenAI itself or any
compatible endpoint. Structured output:

- "json_schema" (default): `response_format` json_schema. Strict mode makes the server
  enforce the schema, but OpenAI accepts it only for schemas where every property is
  required (`is_strict_compatible`). With strict_schema="auto" (default) or True:
  - a schema that already qualifies (planner, replanner) is sent strict, unchanged;
  - otherwise (agent reports, tool steps: optional fields) a strict *wire* schema is
    derived (`strict_wire_schema`): every property becomes required, and optional ones
    that did not accept null become nullable. The model then answers null for "absent";
    `strip_wire_nulls` removes exactly those nulls before the data is returned, so
    NEXUS's Pydantic validation sees the shape its own schema describes. An
    `anyOf` holding both a plain `integer` and a plain `number` (e.g. `StrictInt |
    StrictFloat`) keeps only `number`: the same set of values, but some servers (Groq)
    reject the overlap as ambiguous;
  - a schema that cannot be made strict (e.g. free-form objects) is sent non-strict
    ("auto"), or strict as-is (True, the caller's choice).
  strict_schema=False sends every schema unchanged and non-strict.
- "json_object": for servers without schema support; the schema is added to the system
  prompt as text.

Either way, the response is only a JSON object: callers validate it with Pydantic.
"""

import json
import logging
import re
from typing import Any, Literal

import openai

from app.core.exceptions import LLMContentFilteredError, LLMError, LLMRateLimitError, LLMResponseError, LLMTimeoutError
from app.llm.schemas import LLMRequest, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)

ResponseFormat = Literal["json_schema", "json_object"]
MaxTokensParam = Literal["max_tokens", "max_completion_tokens"]
StrictSchema = bool | Literal["auto"]

# Name sent with the json_schema response format (the request purpose is never sent).
SCHEMA_NAME = "nexus_output"

# Some compatible servers wrap JSON in a single Markdown code fence.
_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)


# Keywords allowed in a strict schema. Anything else (oneOf, allOf, not, patternProperties,
# default, ...) makes "auto" fall back to non-strict rather than risk a 400.
_STRICT_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
     "anyOf", "$ref", "$defs", "description", "title"}
)


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    """Follow a local `#/$defs/...` reference (one level per call; NEXUS schemas are not
    recursive)."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if not ref.startswith("#/$defs/") or ref.removeprefix("#/$defs/") not in defs:
            return node
        node = defs[ref.removeprefix("#/$defs/")]
    return node


def _accepts_null(node: Any, defs: dict[str, Any]) -> bool:
    node = _resolve(node, defs)
    if not isinstance(node, dict):
        return False
    types = node.get("type")
    if types == "null" or (isinstance(types, list) and "null" in types):
        return True
    if "const" in node and node["const"] is None:
        return True
    if None in node.get("enum", ()):
        return True
    return any(_accepts_null(variant, defs) for variant in node.get("anyOf", ()))


def _is_object(node: dict[str, Any]) -> bool:
    types = node.get("type")
    return types == "object" or (isinstance(types, list) and "object" in types)


# Keys a variant may have and still be a plain, unconstrained type.
_PLAIN_KEYS = frozenset({"type", "title", "description"})


def _plain(variant: Any, type_name: str) -> bool:
    return isinstance(variant, dict) and variant.get("type") == type_name and set(variant) <= _PLAIN_KEYS


def _has_numeric_overlap(variants: Any) -> bool:
    return (
        isinstance(variants, list)
        and any(_plain(v, "integer") for v in variants)
        and any(_plain(v, "number") for v in variants)
    )


def _without_integer_overlap(variants: list[Any]) -> list[Any]:
    """`variants` minus its plain `integer` variants when a plain `number` is also present.
    Every integer is a number, so the anyOf accepts exactly the same values; a variant
    with constraints (minimum, enum, ...) is never touched."""
    if not _has_numeric_overlap(variants):
        return variants
    return [v for v in variants if not _plain(v, "integer")]


def _contains_numeric_overlap(node: Any) -> bool:
    if isinstance(node, list):
        return any(_contains_numeric_overlap(item) for item in node)
    if not isinstance(node, dict):
        return False
    return _has_numeric_overlap(node.get("anyOf")) or any(
        _contains_numeric_overlap(value) for value in node.values()
    )


def _tighten(node: Any, defs: dict[str, Any]) -> Any:
    """Copy of `node` where every object lists all properties as required; an optional
    property that does not accept null becomes `anyOf: [<original>, null]`; an anyOf with
    overlapping plain integer and number variants keeps only number. Nothing else (types,
    enums, descriptions, additionalProperties) is changed."""
    if isinstance(node, list):
        return [_tighten(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            required = set(node.get("required", ()))
            out[key] = {
                name: _tighten(schema, defs)
                if name in required or _accepts_null(schema, defs)
                else {"anyOf": [_tighten(schema, defs), {"type": "null"}]}
                for name, schema in value.items()
            }
        elif key == "$defs" and isinstance(value, dict):
            out[key] = {name: _tighten(schema, defs) for name, schema in value.items()}
        elif key == "anyOf":
            out[key] = _without_integer_overlap(_tighten(value, defs))
        elif key == "items":
            out[key] = _tighten(value, defs)
        else:
            out[key] = value
    if _is_object(out) and isinstance(out.get("properties"), dict):
        out["required"] = list(out["properties"])
    return out


def strict_wire_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """The schema to send in strict mode, or None if it cannot be made strict.

    `schema` itself when it already qualifies and has no integer/number overlap; otherwise
    the tightened copy (see `_tighten`) if that qualifies. Responses to a tightened schema
    must go through `strip_wire_nulls(data, schema)` with the ORIGINAL schema (no value
    needs converting back: a JSON number the model sends for a collapsed integer|number
    is an int or a float, both accepted by the original)."""
    if is_strict_compatible(schema) and not _contains_numeric_overlap(schema):
        return schema
    tightened = _tighten(schema, schema.get("$defs", {}))
    return tightened if is_strict_compatible(tightened) else None


def strip_wire_nulls(data: Any, schema: dict[str, Any]) -> Any:
    """Undo the tightening on a response: remove a property whose value is null only when
    the ORIGINAL schema made it optional AND did not accept null for it (i.e. the null can
    only mean "absent"). Every other value, including nulls the original schema allows and
    anything ambiguous, is returned unchanged, so Pydantic still validates it exactly as
    before. Nothing is ever added."""
    return _strip(data, schema, schema.get("$defs", {}))


def _strip(data: Any, node: Any, defs: dict[str, Any]) -> Any:
    node = _resolve(node, defs)
    if not isinstance(node, dict):
        return data
    if "anyOf" in node:
        variant = _matching_variant(data, node["anyOf"], defs)
        return data if variant is None else _strip(data, variant, defs)
    if isinstance(data, dict) and isinstance(node.get("properties"), dict):
        properties = node["properties"]
        required = set(node.get("required", ()))
        out: dict[str, Any] = {}
        for key, value in data.items():
            schema = properties.get(key)
            if schema is None:
                out[key] = value  # unknown key: left for validation to reject
            elif value is None and key not in required and not _accepts_null(schema, defs):
                continue  # null stood for "absent"
            else:
                out[key] = _strip(value, schema, defs)
        return out
    if isinstance(data, list) and "items" in node:
        return [_strip(item, node["items"], defs) for item in data]
    return data


def _matching_variant(data: Any, variants: list[Any], defs: dict[str, Any]) -> Any:
    """The single anyOf variant `data` can belong to, or None if none or several do."""
    if isinstance(data, dict):
        candidates = [v for v in variants if _object_matches(data, _resolve(v, defs), defs)]
    elif isinstance(data, list):
        candidates = [v for v in variants if _resolve(v, defs).get("type") == "array"]
    else:
        return None  # scalars and null have no nested properties to normalize
    return candidates[0] if len(candidates) == 1 else None


def _object_matches(data: dict[str, Any], node: Any, defs: dict[str, Any]) -> bool:
    if not isinstance(node, dict) or not isinstance(node.get("properties"), dict):
        return False
    properties = node["properties"]
    if not set(data) <= set(properties) or not set(node.get("required", ())) <= set(data):
        return False
    for key, value in data.items():
        schema = _resolve(properties[key], defs)
        if not isinstance(schema, dict):
            continue
        if "const" in schema and value != schema["const"]:
            return False
        if "enum" in schema and value not in schema["enum"]:
            return False
    return True


def is_strict_compatible(schema: dict[str, Any]) -> bool:
    """True if `schema` satisfies OpenAI strict structured-output rules: the root is an
    object; every object lists all its properties as required and sets
    `additionalProperties: false`; only supported keywords are used. Conservative: a
    schema this rejects is still sent, just non-strict."""
    if schema.get("type") != "object" or "anyOf" in schema:
        return False
    return _strict_node(schema)


def _strict_node(node: Any) -> bool:
    if not isinstance(node, dict) or not set(node) <= _STRICT_KEYWORDS:
        return False
    types = node.get("type")
    if types == "object" or (isinstance(types, list) and "object" in types):
        properties = node.get("properties")
        if (
            not isinstance(properties, dict)
            or node.get("additionalProperties") is not False
            or set(node.get("required", ())) != set(properties)
        ):
            return False
    children = [
        *node.get("properties", {}).values(),
        *node.get("$defs", {}).values(),
        *node.get("anyOf", ()),
    ]
    if "items" in node:
        children.append(node["items"])
    return all(_strict_node(child) for child in children)


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        api_key: str | None = None,
        base_url: str | None = None,
        response_format: ResponseFormat = "json_schema",
        strict_schema: StrictSchema = "auto",
        max_tokens_param: MaxTokensParam = "max_tokens",
    ) -> None:
        self._model = model
        self._response_format = response_format
        self._strict_schema = strict_schema
        self._max_tokens_param = max_tokens_param
        # api_key/base_url=None let the SDK fall back to OPENAI_API_KEY / OPENAI_BASE_URL
        # in the process environment (and the official endpoint).
        self._client = openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_seconds, max_retries=max_retries
        )

    def _wire_schema(self, schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """(schema to send, strict flag)."""
        if self._strict_schema is False:
            return schema, False
        wire = strict_wire_schema(schema)
        if wire is not None:
            return wire, True
        # Cannot be made strict: "auto" falls back to non-strict; True keeps the caller's
        # explicit choice (the server may reject it).
        return schema, self._strict_schema is True

    def _request_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        system = request.system
        if self._response_format == "json_schema":
            schema, strict = self._wire_schema(request.output_schema)
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "schema": schema, "strict": strict},
            }
        else:
            response_format = {"type": "json_object"}
            system = (
                f"{system}\n\nRespond with a single JSON object (no prose, no code fences) "
                f"that conforms to this JSON schema:\n{json.dumps(request.output_schema)}"
            )
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                *({"role": m.role, "content": m.content} for m in request.messages),
            ],
            "response_format": response_format,
            self._max_tokens_param: request.max_tokens,
        }

    async def generate(self, request: LLMRequest) -> LLMResponse:
        try:
            response = await self._client.chat.completions.create(**self._request_kwargs(request))
        # APITimeoutError subclasses APIConnectionError, so it must be caught first.
        except openai.APITimeoutError as exc:
            raise LLMTimeoutError(f"{request.purpose}: OpenAI-compatible request timed out") from exc
        except openai.RateLimitError as exc:
            raise LLMRateLimitError(
                f"{request.purpose}: the LLM provider is rate-limiting requests for model {self._model!r} "
                f"(HTTP 429, request {exc.request_id}); a provider availability problem, not a task problem"
            ) from exc
        except openai.APIStatusError as exc:
            raise LLMError(
                f"{request.purpose}: OpenAI-compatible API error {exc.status_code} "
                f"({type(exc).__name__}, request {exc.request_id})"
            ) from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"{request.purpose}: could not reach the OpenAI-compatible API") from exc

        if not response.choices:
            raise LLMResponseError(f"{request.purpose}: response contained no choices")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMResponseError(
                f"{request.purpose}: response truncated at max_tokens={request.max_tokens}"
            )
        # Some OpenAI-compatible APIs add a detail: Gemini sends "content_filter: RECITATION".
        finish_reason = str(choice.finish_reason or "")
        if finish_reason.split(":", 1)[0].strip() == "content_filter" or choice.message.refusal:
            reason = f"finish_reason={finish_reason!r}" if finish_reason else "finish_reason=None"
            if choice.message.refusal:
                reason += f", refusal={choice.message.refusal[:200]!r}"
            raise LLMContentFilteredError(
                f"{request.purpose}: the model declined the request or its output was blocked by a content filter ({reason})",
                provider_reason=reason,
            )

        text = choice.message.content
        if not text:
            raise LLMResponseError(f"{request.purpose}: response contained no text")
        text = text.strip()
        if match := _FENCE.match(text):
            text = match.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"{request.purpose}: response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise LLMResponseError(f"{request.purpose}: response is not a JSON object")
        if self._response_format == "json_schema":
            schema, _ = self._wire_schema(request.output_schema)
            if schema is not request.output_schema:
                data = strip_wire_nulls(data, request.output_schema)

        usage = response.usage
        model = response.model or self._model
        logger.info(
            "%s: model=%s input_tokens=%s output_tokens=%s request=%s",
            request.purpose,
            model,
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
            getattr(response, "_request_id", None),
        )
        return LLMResponse(
            data=data,
            provider=self.name,
            model=model,
            usage=LLMUsage(
                input_tokens=usage.prompt_tokens if usage else None,
                output_tokens=usage.completion_tokens if usage else None,
            ),
        )
