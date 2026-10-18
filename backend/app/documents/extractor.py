"""Document text extraction engine supporting text, markdown, pdf, json, and csv."""

import io
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_DOCUMENT_CHARS = 200_000
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB limit


class DocumentExtractionError(Exception):
    """Raised when text cannot be extracted from a document."""


def extract_text_from_bytes(
    filename: str, content: bytes, content_type: str | None = None
) -> str:
    """Extract clean text content from file bytes based on filename extension and content-type."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise DocumentExtractionError(
            f"File '{filename}' exceeds maximum allowed size of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
        )

    suffix = Path(filename).suffix.lower()
    ct = (content_type or "").lower()

    if suffix == ".pdf" or "application/pdf" in ct:
        text = _extract_from_pdf(content, filename)
    elif suffix == ".json" or "application/json" in ct:
        text = _extract_from_json(content, filename)
    elif suffix == ".csv" or "text/csv" in ct:
        text = _extract_from_csv(content, filename)
    else:
        text = _decode_text_bytes(content, filename)

    text = text.strip()
    if not text:
        raise DocumentExtractionError(f"No readable text could be extracted from '{filename}'")

    if len(text) > MAX_DOCUMENT_CHARS:
        text = (
            text[:MAX_DOCUMENT_CHARS]
            + f"\n\n[Content truncated: document exceeded {MAX_DOCUMENT_CHARS} characters limit]"
        )

    return text


def _extract_from_pdf(content: bytes, filename: str) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages_text: list[str] = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            page_text = page_text.strip()
            if page_text:
                pages_text.append(f"--- Page {i + 1} ---\n{page_text}")
        return "\n\n".join(pages_text)
    except Exception as exc:
        logger.warning("Failed to parse PDF '%s': %s", filename, exc)
        raise DocumentExtractionError(f"Failed to extract text from PDF '{filename}': {exc}") from exc


def _extract_from_json(content: bytes, filename: str) -> str:
    raw = _decode_text_bytes(content, filename)
    try:
        parsed = json.loads(raw)
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except Exception:
        return raw


def _extract_from_csv(content: bytes, filename: str) -> str:
    return _decode_text_bytes(content, filename)


def _decode_text_bytes(content: bytes, filename: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")
