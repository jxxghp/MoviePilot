import asyncio
import json
import uuid
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Header, Security
from fastapi.responses import JSONResponse, StreamingResponse

from app.schemas.openai import AnthropicErrorDetail as _SchemaAnthropicErrorDetail
from app.schemas.openai import AnthropicErrorResponse as _SchemaAnthropicErrorResponse
from app.schemas.openai import AnthropicMessagesRequest as _SchemaAnthropicMessagesRequest
from app.schemas.openai import AnthropicMessagesResponse as _SchemaAnthropicMessagesResponse
from app.schemas.openai import AnthropicTextBlock as _SchemaAnthropicTextBlock
from app.api.endpoints.openai import (
    MODEL_ID,
    _is_manager_unavailable,
    _run_managed_agent,
)
from app.api.openai_utils import (
    build_anthropic_messages,
    build_prompt,
    build_session_id,
)
from app.agent.runtime_loader import get_running_agent_manager
from app.runtime.config import settings
from app.application.security.access import anthropic_api_key_header

ANTHROPIC_ERROR_RESPONSES = {
    400: {"model": _SchemaAnthropicErrorResponse, "description": "请求格式错误"},
    401: {"model": _SchemaAnthropicErrorResponse, "description": "认证失败"},
    422: {"model": _SchemaAnthropicErrorResponse, "description": "请求参数校验失败"},
    500: {"model": _SchemaAnthropicErrorResponse, "description": "服务内部错误"},
    503: {"model": _SchemaAnthropicErrorResponse, "description": "AI Agent 不可用"},
}

router = APIRouter(responses=ANTHROPIC_ERROR_RESPONSES)

SESSION_PREFIX = "anthropic:"


def _anthropic_error_response(
    message: str,
    status_code: int,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_SchemaAnthropicErrorResponse(
            error=_SchemaAnthropicErrorDetail(type=error_type, message=message)
        ).model_dump(),
    )


def _check_auth(api_key: Optional[str]) -> Optional[JSONResponse]:
    """
    Anthropic 兼容接口以 API_TOKEN 认证受信客户端，认证通过即按管理员级 Agent 集成处理。
    """
    if not api_key or api_key != settings.API_TOKEN:
        return _anthropic_error_response(
            "invalid x-api-key",
            401,
            error_type="authentication_error",
        )
    return None


async def _stream_anthropic_response(
    manager,
    session_id: str,
    user_id: str,
    prompt: str,
    images: List[str],
) -> AsyncIterator[str]:
    event_queue: asyncio.Queue = asyncio.Queue()

    message_id = f"msg_{uuid.uuid4().hex}"

    async def _run_agent():
        try:
            await _run_managed_agent(
                manager=manager,
                session_id=session_id,
                user_id=user_id,
                username="anthropic-client",
                source="anthropic",
                prompt=prompt,
                images=images,
                stream_mode=True,
                event_queue=event_queue,
            )
        except asyncio.CancelledError:
            await event_queue.put({"error": "MoviePilot AI agent is unavailable."})
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
                yield (
                    "event: error\n"
                    f"data: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(item['error'])}}, ensure_ascii=False)}\n\n"
                )
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"
                return
            text = str(item or "")
            if not text:
                continue
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}, ensure_ascii=False)}\n\n"
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0}, ensure_ascii=False)}\n\n"
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 0}}, ensure_ascii=False)}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"
    finally:
        await manager.clear_session(session_id=session_id, user_id=user_id)
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@router.post(
    "/messages",
    summary="Anthropic compatible messages",
    response_model=_SchemaAnthropicMessagesResponse,
    responses={
        200: {
            "description": "Anthropic message 或 SSE 数据流",
            "content": {
                "text/event-stream": {"schema": {"type": "string"}},
            },
        }
    },
)
async def messages(
    payload: _SchemaAnthropicMessagesRequest,
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
    manager = get_running_agent_manager()
    if manager is None:
        return _anthropic_error_response(
            "MoviePilot AI agent is unavailable.",
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
    if payload.stream:
        return StreamingResponse(
            _stream_anthropic_response(
                manager=manager,
                session_id=session_id,
                user_id=session_id,
                prompt=prompt,
                images=images,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    collected_messages = []
    try:
        result, collected_messages = await _run_managed_agent(
            manager=manager,
            session_id=session_id,
            user_id=session_id,
            username="anthropic-client",
            source="anthropic",
            prompt=prompt,
            images=images,
            stream_mode=False,
        )
    except Exception as exc:
        if _is_manager_unavailable(exc):
            return _anthropic_error_response(
                "MoviePilot AI agent is unavailable.",
                503,
                error_type="api_error",
            )
        return _anthropic_error_response(str(exc), 500, error_type="api_error")
    finally:
        await manager.clear_session(session_id=session_id, user_id=session_id)

    content = "\n\n".join(
        message.strip()
        for message in collected_messages
        if message and message.strip()
    ).strip()
    if not content and result:
        content = str(result).strip()
    if not content:
        content = "未获得有效回复。"

    return _SchemaAnthropicMessagesResponse(
        id=f"msg_{uuid.uuid4().hex}",
        content=[_SchemaAnthropicTextBlock(text=content)],
        model=MODEL_ID,
    )
