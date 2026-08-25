import asyncio
import uuid
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, Header, Security
from fastapi.responses import JSONResponse

from app.adapters.web.security.access import anthropic_api_key_header
from app.api.context import (
    get_background_task_registry_compat,
    resolve_background_task_registry,
)
from app.api.endpoints.openai import (
    MODEL_ID,
    _is_manager_queue_full,
    _is_manager_unavailable,
    _run_managed_agent,
)
from app.api.openai_utils import (
    build_anthropic_messages,
    build_prompt,
    build_session_id,
)
from app.api.presentation.sse import build_sse_response, encode_named_event
from app.application.agent import get_running_agent_manager
from app.application.configuration import get_api_runtime_config_snapshot
from app.runtime.tasks import TaskRegistry
from app.schemas.openai import AnthropicErrorDetail as _SchemaAnthropicErrorDetail
from app.schemas.openai import AnthropicErrorResponse as _SchemaAnthropicErrorResponse
from app.schemas.openai import AnthropicMessagesRequest as _SchemaAnthropicMessagesRequest
from app.schemas.openai import AnthropicMessagesResponse as _SchemaAnthropicMessagesResponse
from app.schemas.openai import AnthropicTextBlock as _SchemaAnthropicTextBlock

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
    if not api_key or api_key != get_api_runtime_config_snapshot().api_token:
        return _anthropic_error_response(
            "invalid x-api-key",
            401,
            error_type="authentication_error",
        )
    return None


def _manager_execution_error(error: BaseException) -> JSONResponse:
    """把 AgentManager 稳定错误映射为 Anthropic 兼容错误响应。"""
    if _is_manager_unavailable(error):
        return _anthropic_error_response(
            "MoviePilot AI agent is unavailable.",
            503,
            error_type="api_error",
        )
    if _is_manager_queue_full(error):
        return _anthropic_error_response(
            str(error),
            429,
            error_type="rate_limit_error",
        )
    return _anthropic_error_response(str(error), 500, error_type="api_error")


async def _stream_anthropic_response(
    manager,
    session_id: str,
    user_id: str,
    prompt: str,
    images: List[str],
    task_registry: TaskRegistry | None = None,
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

    task = resolve_background_task_registry(task_registry).create(
        _run_agent(),
        owner="api.anthropic.stream",
    )
    try:
        yield encode_named_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": MODEL_ID,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        yield encode_named_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        while True:
            item = await event_queue.get()
            if item is None:
                break
            if isinstance(item, dict) and item.get("error"):
                yield encode_named_event(
                    "error",
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": str(item["error"]),
                        },
                    },
                )
                yield encode_named_event("message_stop", {"type": "message_stop"})
                return
            text = str(item or "")
            if not text:
                continue
            yield encode_named_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        yield encode_named_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        )
        yield encode_named_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        )
        yield encode_named_event("message_stop", {"type": "message_stop"})
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
    task_registry: TaskRegistry = Depends(get_background_task_registry_compat),
):
    auth_error = _check_auth(x_api_key)
    if auth_error:
        return auth_error

    if not get_api_runtime_config_snapshot().ai_agent_enable:
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
        return build_sse_response(
            _stream_anthropic_response(
                manager=manager,
                session_id=session_id,
                user_id=session_id,
                prompt=prompt,
                images=images,
                task_registry=task_registry,
            ),
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
        return _manager_execution_error(exc)
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
