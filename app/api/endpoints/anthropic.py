import asyncio
import json
import uuid
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Header, Security
from fastapi.responses import JSONResponse, StreamingResponse

from app import schemas
from app.api.endpoints.openai import (
    MODEL_ID,
    _CollectingMoviePilotAgent,
)
from app.api.openai_utils import (
    build_anthropic_messages,
    build_prompt,
    build_session_id,
)
from app.core.config import settings
from app.core.security import anthropic_api_key_header
from app.schemas.types import MessageChannel

router = APIRouter()

SESSION_PREFIX = "anthropic:"


def _anthropic_error_response(
    message: str,
    status_code: int,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=schemas.AnthropicErrorResponse(
            error=schemas.AnthropicErrorDetail(type=error_type, message=message)
        ).model_dump(),
    )


def _check_auth(api_key: Optional[str]) -> Optional[JSONResponse]:
    if not api_key or api_key != settings.API_TOKEN:
        return _anthropic_error_response(
            "invalid x-api-key",
            401,
            error_type="authentication_error",
        )
    return None


async def _stream_anthropic_response(
    agent: _CollectingMoviePilotAgent,
    prompt: str,
    images: List[str],
) -> AsyncIterator[str]:
    event_queue: asyncio.Queue = asyncio.Queue()
    if hasattr(agent.stream_handler, "bind_queue"):
        agent.stream_handler.bind_queue(event_queue)

    message_id = f"msg_{uuid.uuid4().hex}"

    async def _run_agent():
        try:
            await agent.process(prompt, images=images, files=None)
        except Exception as exc:
            await event_queue.put({"error": str(exc)})
        finally:
            await event_queue.put(None)

    task = asyncio.create_task(_run_agent())
    try:
        yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': message_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': MODEL_ID, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"
        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n"
        while True:
            item = await event_queue.get()
            if item is None:
                break
            if isinstance(item, dict) and item.get("error"):
                raise RuntimeError(str(item["error"]))
            text = str(item or "")
            if not text:
                continue
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}, ensure_ascii=False)}\n\n"
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0}, ensure_ascii=False)}\n\n"
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 0}}, ensure_ascii=False)}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@router.post(
    "/messages",
    summary="Anthropic compatible messages",
    response_model=schemas.AnthropicMessagesResponse,
)
async def messages(
    payload: schemas.AnthropicMessagesRequest,
    x_api_key: Optional[str] = Security(anthropic_api_key_header),
    anthropic_version: Optional[str] = Header(default=None, alias="anthropic-version"),
):
    auth_error = _check_auth(x_api_key)
    if auth_error:
        return auth_error

    if not settings.AI_AGENT_ENABLE:
        return _anthropic_error_response(
            "MoviePilot AI agent is disabled.",
            503,
            error_type="api_error",
        )

    normalized_messages = build_anthropic_messages(payload.system, payload.messages)
    try:
        prompt, images = build_prompt(normalized_messages, use_server_session=False)
    except ValueError as exc:
        return _anthropic_error_response(str(exc), 400)

    session_seed = anthropic_version or "anthropic"
    session_id = build_session_id(f"{session_seed}:{uuid.uuid4().hex}", SESSION_PREFIX)
    agent = _CollectingMoviePilotAgent(
        session_id=session_id,
        user_id=session_id,
        channel=MessageChannel.Web.value,
        source="anthropic",
        username="anthropic-client",
        stream_mode=payload.stream,
    )

    if payload.stream:
        return StreamingResponse(
            _stream_anthropic_response(agent=agent, prompt=prompt, images=images),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        result = await agent.process(prompt, images=images, files=None)
    except Exception as exc:
        return _anthropic_error_response(str(exc), 500, error_type="api_error")

    content = "\n\n".join(
        message.strip()
        for message in agent.collected_messages
        if message and message.strip()
    ).strip()
    if not content and result:
        content = str(result).strip()
    if not content:
        content = "未获得有效回复。"

    return schemas.AnthropicMessagesResponse(
        id=f"msg_{uuid.uuid4().hex}",
        content=[schemas.AnthropicTextBlock(text=content)],
        model=MODEL_ID,
    )
