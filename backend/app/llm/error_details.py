"""Small, safe excerpts from provider API error bodies."""

import json
from typing import Any

from app.core.redaction import safe_message


def provider_error_detail(body: Any) -> str | None:
    """Extract bounded diagnostic fields without copying an entire provider payload.

    Provider error bodies are untrusted and may contain echoed request data. Only
    conventional error metadata is retained, then redacted and capped.
    """
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return None
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    if not isinstance(error, dict):
        return None
    fields = ("message", "code", "type", "param")
    parts = [f"{name}={error[name]}" for name in fields if isinstance(error.get(name), (str, int, float))]
    return safe_message("; ".join(parts), max_chars=500, default="") or None
