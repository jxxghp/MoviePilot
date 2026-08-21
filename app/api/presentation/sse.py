"""SSE wire mapping and response lifecycle helpers."""

import json
from collections.abc import AsyncIterable, Iterable
from typing import Any

from fastapi.responses import StreamingResponse


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def encode_data_event(payload: dict[str, Any]) -> str:
    """Encode an OpenAI-style unnamed SSE data event."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def encode_named_event(event: str, payload: dict[str, Any]) -> str:
    """Encode a named SSE event without applying a response envelope."""
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def build_sse_response(
    content: AsyncIterable[str] | Iterable[str],
    *,
    headers: dict[str, str] | None = None,
) -> StreamingResponse:
    """Build a non-buffered SSE response and merge protocol-specific headers."""
    return StreamingResponse(
        content,
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **(headers or {})},
    )


def build_sse_error_response(payload: str) -> StreamingResponse:
    """Build a one-event SSE error response using the common transport policy."""
    return build_sse_response(iter([payload]))
