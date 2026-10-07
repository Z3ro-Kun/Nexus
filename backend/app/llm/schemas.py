"""Provider-neutral request/response types and JSON-schema preparation."""

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError


class LLMMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    """One structured-output call. The response must be a JSON object matching
    `output_schema`; callers still validate it themselves (provider output is untrusted)."""

    model_config = ConfigDict(frozen=True)

    # Identifies the caller, e.g. "planner" or "agent:researcher". Used for logging and
    # by the fake provider to pick a scripted reply. Never sent to the provider.
    purpose: str
    system: str
    messages: list[LLMMessage] = Field(min_length=1)
    output_schema: dict[str, Any]
    max_tokens: int = Field(default=16000, ge=1)
    # Caller-side labels (task id, agent type, ...). Never sent to the provider.
    metadata: dict[str, str] = Field(default_factory=dict)


class LLMUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: dict[str, JsonValue]
    provider: str
    model: str
    usage: LLMUsage = LLMUsage()


# Keywords the structured-output JSON-schema subset does not support. They are removed
# from the schema sent to the provider and enforced afterwards by Pydantic validation.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "title",
        "default",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    }
)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for `model`, restricted to the structured-output subset.

    Every object gets `additionalProperties: false`, and unsupported constraint keywords
    are dropped. Dropped constraints are not lost: responses are validated against the
    Pydantic model afterwards.
    """
    return _strip(copy.deepcopy(model.model_json_schema()))


# Keys whose values map *names* to schemas; the names themselves are never keywords.
_NAMED_SCHEMA_MAPS = frozenset({"properties", "$defs"})


def _strip(node: Any) -> Any:
    if isinstance(node, list):
        return [_strip(item) for item in node]
    if not isinstance(node, dict):
        return node
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key in _NAMED_SCHEMA_MAPS:
            cleaned[key] = {name: _strip(schema) for name, schema in value.items()}
        elif key not in _UNSUPPORTED_KEYWORDS:
            cleaned[key] = _strip(value)
    if cleaned.get("type") == "object":
        cleaned["additionalProperties"] = False
    return cleaned


def describe_validation_error(exc: ValidationError) -> str:
    """Short description of the first validation error, for error messages."""
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"]) or "<root>"
    return f"{exc.error_count()} validation errors; first: {location}: {error['msg']}"


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of `schema` with local `#/$defs/...` references replaced by their definitions
    and `$defs` removed, so it can be embedded inside another schema. Recursive schemas
    are not supported (and are not used by NEXUS)."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return resolve(defs[ref.removeprefix("#/$defs/")])
        return {key: resolve(value) for key, value in node.items() if key != "$defs"}

    return resolve(schema)
