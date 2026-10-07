"""Safe error messages: failure text is truncated and obvious credentials are redacted
before it is written to an event (events are permanent and exposed through the API).

This is a best-effort filter for common secret shapes, not a guarantee; the primary
defense remains that credentials are never put into error messages in the first place.
"""

import re

MAX_ERROR_CHARS = 2000
REDACTED = "[REDACTED]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # user:password@ in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), rf"\1{REDACTED}@"),
    # Authorization headers / bearer tokens
    (re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    # key=value / key: value for credential-like names (also in query strings)
    (
        re.compile(
            r"(?i)\b([a-z0-9_-]*(?:api[_-]?key|secret|token|password|passwd|pwd|auth)[a-z0-9_-]*)"
            r"(\s*[=:]\s*|=)(\"[^\"]*\"|'[^']*'|[^\s&,;\"']+)"
        ),
        rf"\1\2{REDACTED}",
    ),
    # Well-known key prefixes (Anthropic, OpenAI-style, GitHub, AWS access keys, Slack)
    (re.compile(r"\b(sk-[a-z0-9-]{0,20}[A-Za-z0-9_-]{16,})"), REDACTED),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"), REDACTED),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), REDACTED),
    (re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})"), REDACTED),
)


def safe_message(message: str | None, *, max_chars: int = MAX_ERROR_CHARS, default: str = "task failed") -> str:
    """Redact credential-like substrings and truncate. Never returns an empty string."""
    text = (message or "").strip() or default
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > max_chars:
        text = text[: max_chars - 15].rstrip() + " …[truncated]"
    return text
