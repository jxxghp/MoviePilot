import asyncio
import json
import time
import uuid
from threading import Lock
from typing import AsyncIterator, List, Optional, Tuple

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.schemas.openai import OpenAIChatCompletionResponse as _SchemaOpenAIChatCompletionResponse
from app.schemas.openai import OpenAIChatCompletionsRequest as _SchemaOpenAIChatCompletionsRequest
from app.schemas.openai import OpenAIErrorDetail as _SchemaOpenAIErrorDetail
from app.schemas.openai import OpenAIErrorResponse as _SchemaOpenAIErrorResponse
from app.schemas.openai import OpenAIModelInfo as _SchemaOpenAIModelInfo
from app.schemas.openai import OpenAIModelListResponse as _SchemaOpenAIModelListResponse
from app.schemas.openai import OpenAIResponsesOutputMessage as _SchemaOpenAIResponsesOutputMessage
from app.schemas.openai import OpenAIResponsesOutputText as _SchemaOpenAIResponsesOutputText
from app.schemas.openai import OpenAIResponsesRequest as _SchemaOpenAIResponsesRequest
from app.schemas.openai import OpenAIResponsesResponse as _SchemaOpenAIResponsesResponse
from app.schemas.openai import OpenAIUsage as _SchemaOpenAIUsage
from app.api.openai_utils import (
    build_completion_payload,
    build_prompt,
    build_responses_input,
    build_session_id,
)
from app.agent.runtime_loader import (
    get_moviepilot_agent_type,
    get_running_agent_manager,
)
from app.agent.contracts import ReplyMode
from app.runtime.config import settings
from app.application.security.access import openai_bearer_scheme
from app.schemas.types import NotificationChannel

OPENAI_ERROR_RESPONSES = {
    400: {"model": _SchemaOpenAIErrorResponse, "description": "请求格式错误"},
    401: {"model": _SchemaOpenAIErrorResponse, "description": "认证失败"},
    422: {"model": _SchemaOpenAIErrorResponse, "description": "请求参数校验失败"},
    500: {"model": _SchemaOpenAIErrorResponse, "description": "服务内部错误"},
    503: {"model": _SchemaOpenAIErrorResponse, "description": "AI Agent 不可用"},
}

router = APIRouter(responses=OPENAI_ERROR_RESPONSES)

MODEL_ID = "moviepilot-agent"
SESSION_PREFIX = "openai:"


class _CollectingMoviePilotAgentMixin:
    """
    捕获 Agent 最终输出，避免再通过消息渠道二次发送。
    """

    def __init__(self, *args, stream_mode: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.collected_messages: List[str] = []
        self.stream_mode = stream_mode
        if stream_mode:
            self.stream_handler = _get_openai_streaming_handler_type()()

    def _should_stream(self) -> bool:
        return self.stream_mode

    def configure_protocol_request(
        self,
        *,
        stream_mode: bool,
        event_queue: Optional[asyncio.Queue],
    ) -> None:
        """切换请求级输出目标，并保持已编译工具引用的 handler identity。"""
        self.collected_messages = []
        self.stream_mode = stream_mode
        if isinstance(self.stream_handler, _OpenAIStreamingHandlerMixin):
            self.stream_handler.bind_queue(event_queue if stream_mode else None)
            return
        if not stream_mode:
            return
        self.stream_handler = _get_openai_streaming_handler_type()()
        self.stream_handler.bind_queue(event_queue)
        # 已编译工具持有旧 handler；identity 变化时必须重建图和工具目录。
        self._compiled_agent_bundle = None

    def release_protocol_request(
        self,
        event_queue: Optional[asyncio.Queue],
    ) -> None:
        """释放已结束请求的输出队列，不影响同会话已重绑的新请求。"""
        if isinstance(self.stream_handler, _OpenAIStreamingHandlerMixin):
            self.stream_handler.unbind_queue(event_queue)

    async def send_agent_message(self, message: str, title: str = ""):
        text = (message or "").strip()
        if title and text:
            text = f"{title}\n{text}"
        elif title:
            text = title.strip()
        if text:
            self.collected_messages.append(text)
            if self.stream_mode:
                self.stream_handler.emit(text)


class _OpenAIStreamingHandlerMixin:
    """
    将 Agent 流式输出转发到 OpenAI SSE 队列，不向站内消息系统落消息。
    """

    def __init__(self):
        super().__init__()
        self._event_queue: Optional[asyncio.Queue] = None

    def bind_queue(self, queue: Optional[asyncio.Queue]):
        """绑定当前协议请求的输出队列。"""
        self._event_queue = queue

    def unbind_queue(self, queue: Optional[asyncio.Queue]) -> None:
        """仅当仍指向该请求时解除绑定，避免清掉已排队的新请求。"""
        if self._event_queue is queue:
            self._event_queue = None

    def emit(self, token: str):
        emitted = super().emit(token)
        if emitted and self._event_queue is not None:
            self._event_queue.put_nowait(emitted)

    def flush_pending_tool_summary(self) -> str:
        emitted = super().flush_pending_tool_summary()
        if emitted and self._event_queue is not None:
            self._event_queue.put_nowait(emitted)
        return emitted

    async def start_streaming(
        self,
        channel: Optional[str] = None,
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        original_message_id: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        title: str = "",
    ):
        self._channel = channel
        self._source = source
        self._user_id = user_id
        self._username = username
        self._original_message_id = original_message_id
        self._original_chat_id = original_chat_id
        self._title = title
        self._streaming_enabled = True
        self._sent_text = ""
        self._message_response = None
        self._msg_start_offset = 0
        self._max_message_length = 0

    async def stop_streaming(self) -> Tuple[bool, str]:
        if not self._streaming_enabled:
            return False, ""
        self._streaming_enabled = False
        with self._lock:
            final_text = self._buffer
            self._buffer = ""
            self._sent_text = ""
            self._message_response = None
            self._msg_start_offset = 0
        return True, final_text


def _get_openai_streaming_handler_type() -> type:
    """首次兼容协议调用时才解析完整流式处理器。"""
    global _OPENAI_STREAMING_HANDLER_TYPE
    if _OPENAI_STREAMING_HANDLER_TYPE is not None:
        return _OPENAI_STREAMING_HANDLER_TYPE
    with _OPENAI_STREAMING_HANDLER_TYPE_LOCK:
        if _OPENAI_STREAMING_HANDLER_TYPE is None:
            from app.agent.callback import StreamingHandler

            _OPENAI_STREAMING_HANDLER_TYPE = type(
                "_RuntimeOpenAIStreamingHandler",
                (_OpenAIStreamingHandlerMixin, StreamingHandler),
                {"__module__": __name__},
            )
        return _OPENAI_STREAMING_HANDLER_TYPE


_OPENAI_STREAMING_HANDLER_TYPE_LOCK = Lock()
_OPENAI_STREAMING_HANDLER_TYPE: Optional[type] = None


def _build_collecting_agent_type(agent_base_type: type) -> type:
    """为 OpenAI 与 Anthropic 兼容协议组合唯一的运行时类型。"""
    return type(
        "_RuntimeCollectingMoviePilotAgent",
        (_CollectingMoviePilotAgentMixin, agent_base_type),
        {"__module__": __name__},
    )


_COLLECTING_AGENT_TYPE_LOCK = Lock()
_COLLECTING_AGENT_TYPE: Optional[type] = None


def _get_collecting_agent_type() -> type:
    """在首个真实兼容协议请求边界 single-flight 解析 Agent 类型。"""
    global _COLLECTING_AGENT_TYPE
    if _COLLECTING_AGENT_TYPE is not None:
        return _COLLECTING_AGENT_TYPE
    with _COLLECTING_AGENT_TYPE_LOCK:
        if _COLLECTING_AGENT_TYPE is None:
            _COLLECTING_AGENT_TYPE = _build_collecting_agent_type(
                get_moviepilot_agent_type()
            )
        return _COLLECTING_AGENT_TYPE


def _sse_payload(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_response(
    manager,
    session_id: str,
    user_id: str,
    username: str,
    prompt: str,
    images: List[str],
    cleanup_session: bool,
) -> AsyncIterator[str]:
    event_queue: asyncio.Queue = asyncio.Queue()

    created = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    finished = False

    async def _run_agent():
        try:
            await _run_managed_agent(
                manager=manager,
                session_id=session_id,
                user_id=user_id,
                username=username,
                source="openai",
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
        yield _sse_payload(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
        )

        while True:
            item = await event_queue.get()
            if item is None:
                break
            if isinstance(item, dict) and item.get("error"):
                yield _sse_payload(
                    {
                        "error": {
                            "message": str(item["error"]),
                            "type": "server_error",
                            "code": "agent_execution_failed",
                        }
                    }
                )
                yield "data: [DONE]\n\n"
                return
            text = str(item or "")
            if not text:
                continue
            yield _sse_payload(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text},
                            "finish_reason": None,
                        }
                    ],
                }
            )

        finished = True
        yield _sse_payload(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }
        )
        yield "data: [DONE]\n\n"
    finally:
        if cleanup_session:
            await manager.clear_session(session_id=session_id, user_id=user_id)
        elif not task.done():
            await manager.stop_current_task(session_id)
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        elif finished:
            await task


def _is_manager_unavailable(error: BaseException) -> bool:
    """识别 manager acceptance gate 的稳定错误，不导入完整编排模块。"""
    return getattr(error, "code", None) == "agent_manager_unavailable"


async def _run_managed_agent(
    *,
    manager,
    session_id: str,
    user_id: str,
    username: str,
    source: str,
    prompt: str,
    images: List[str],
    stream_mode: bool,
    event_queue: Optional[asyncio.Queue] = None,
) -> tuple[str, List[str]]:
    """通过 AgentManager 执行协议请求，并在 worker 内配置请求级输出。"""
    agent_holder = {}

    def configure_agent(agent) -> None:
        agent.configure_protocol_request(
            stream_mode=stream_mode,
            event_queue=event_queue,
        )
        agent_holder["agent"] = agent

    try:
        result = await manager.process_message(
            session_id=session_id,
            user_id=user_id,
            message=prompt,
            images=images,
            files=None,
            channel=NotificationChannel.Web.value,
            source=source,
            username=username,
            reply_mode=ReplyMode.CAPTURE_ONLY,
            allow_message_tools=True,
            agent_factory=_get_collecting_agent_type(),
            agent_setup=configure_agent,
            wait_for_completion=True,
        )
        agent = agent_holder.get("agent")
        return result, list(agent.collected_messages if agent else [])
    finally:
        agent = agent_holder.get("agent")
        if agent is not None:
            agent.release_protocol_request(event_queue)


def _error_response(
    message: str,
    status_code: int,
    error_type: str = "invalid_request_error",
    code: Optional[str] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_SchemaOpenAIErrorResponse(
            error=_SchemaOpenAIErrorDetail(
                message=message,
                type=error_type,
                code=code,
            )
        ).model_dump(),
        headers={"WWW-Authenticate": "Bearer"},
    )


def _check_auth(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[JSONResponse]:
    """
    OpenAI 兼容接口以 API_TOKEN 认证受信客户端，认证通过即按管理员级 Agent 集成处理。
    """
    if not credentials or credentials.scheme.lower() != "bearer":
        return _error_response(
            "Invalid bearer token.",
            401,
            error_type="authentication_error",
            code="invalid_api_key",
        )
    if credentials.credentials != settings.API_TOKEN:
        return _error_response(
            "Invalid bearer token.",
            401,
            error_type="authentication_error",
            code="invalid_api_key",
        )
    return None


@router.get(
    "/models",
    summary="OpenAI compatible models",
    response_model=_SchemaOpenAIModelListResponse,
)
async def list_models(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(
        openai_bearer_scheme
    ),
):
    auth_error = _check_auth(credentials)
    if auth_error:
        return auth_error
    now = int(time.time())
    return _SchemaOpenAIModelListResponse(
        data=[_SchemaOpenAIModelInfo(id=MODEL_ID, created=now)]
    )


@router.post(
    "/chat/completions",
    summary="OpenAI compatible chat completions",
    response_model=_SchemaOpenAIChatCompletionResponse,
    responses={
        200: {
            "description": "OpenAI chat completion 或 SSE 数据流",
            "content": {
                "text/event-stream": {"schema": {"type": "string"}},
            },
        }
    },
)
async def chat_completions(
    payload: _SchemaOpenAIChatCompletionsRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(
        openai_bearer_scheme
    ),
):
    auth_error = _check_auth(credentials)
    if auth_error:
        return auth_error

    if not settings.AI_AGENT_ENABLE:
        return _error_response(
            "MoviePilot AI agent is disabled.",
            503,
            error_type="server_error",
            code="ai_agent_disabled",
        )
    manager = get_running_agent_manager()
    if manager is None:
        return _error_response(
            "MoviePilot AI agent is unavailable.",
            503,
            error_type="server_error",
            code="ai_agent_unavailable",
        )

    if not payload.messages:
        return _error_response(
            "`messages` must be a non-empty array.",
            400,
            code="invalid_messages",
        )

    session_key = (
        str(payload.user or "").strip()
        or str(request.headers.get("x-session-id") or "").strip()
        or str(uuid.uuid4())
    )
    use_server_session = bool(
        str(payload.user or "").strip()
        or str(request.headers.get("x-session-id") or "").strip()
    )

    try:
        prompt, images = build_prompt(
            payload.messages, use_server_session=use_server_session
        )
    except ValueError as exc:
        return _error_response(str(exc), 400, code="invalid_messages")

    session_id = build_session_id(session_key, SESSION_PREFIX)
    username = str(payload.user or "openai-client")
    if payload.stream:
        return StreamingResponse(
            _stream_response(
                manager=manager,
                session_id=session_id,
                user_id=session_key,
                username=username,
                prompt=prompt,
                images=images,
                cleanup_session=not use_server_session,
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
            user_id=session_key,
            username=username,
            source="openai",
            prompt=prompt,
            images=images,
            stream_mode=False,
        )
    except Exception as exc:
        if _is_manager_unavailable(exc):
            return _error_response(
                "MoviePilot AI agent is unavailable.",
                503,
                error_type="server_error",
                code="ai_agent_unavailable",
            )
        return _error_response(
            str(exc),
            500,
            error_type="server_error",
            code="agent_execution_failed",
        )
    finally:
        if not use_server_session:
            await manager.clear_session(session_id=session_id, user_id=session_key)

    content = "\n\n".join(
        message.strip()
        for message in collected_messages
        if message and message.strip()
    ).strip()
    if not content and result:
        content = str(result).strip()
    if not content:
        content = "未获得有效回复。"

    return JSONResponse(content=build_completion_payload(content, MODEL_ID))


@router.post(
    "/responses",
    summary="OpenAI compatible responses",
    response_model=_SchemaOpenAIResponsesResponse,
)
async def responses(
    payload: _SchemaOpenAIResponsesRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(
        openai_bearer_scheme
    ),
):
    auth_error = _check_auth(credentials)
    if auth_error:
        return auth_error

    if not settings.AI_AGENT_ENABLE:
        return _error_response(
            "MoviePilot AI agent is disabled.",
            503,
            error_type="server_error",
            code="ai_agent_disabled",
        )
    manager = get_running_agent_manager()
    if manager is None:
        return _error_response(
            "MoviePilot AI agent is unavailable.",
            503,
            error_type="server_error",
            code="ai_agent_unavailable",
        )

    if payload.stream:
        return _error_response(
            "Streaming is not supported for /responses yet.",
            400,
            code="unsupported_stream",
        )

    normalized_messages = build_responses_input(
        payload.input, instructions=payload.instructions
    )
    if not normalized_messages:
        return _error_response(
            "`input` must include at least one usable message.",
            400,
            code="invalid_input",
        )

    try:
        prompt, images = build_prompt(
            normalized_messages, use_server_session=bool(payload.user)
        )
    except ValueError as exc:
        return _error_response(str(exc), 400, code="invalid_input")

    session_key = str(payload.user or uuid.uuid4())
    session_id = build_session_id(session_key, SESSION_PREFIX)
    collected_messages = []
    try:
        result, collected_messages = await _run_managed_agent(
            manager=manager,
            session_id=session_id,
            user_id=session_key,
            username=str(payload.user or "openai-client"),
            source="openai.responses",
            prompt=prompt,
            images=images,
            stream_mode=False,
        )
    except Exception as exc:
        if _is_manager_unavailable(exc):
            return _error_response(
                "MoviePilot AI agent is unavailable.",
                503,
                error_type="server_error",
                code="ai_agent_unavailable",
            )
        return _error_response(
            str(exc),
            500,
            error_type="server_error",
            code="agent_execution_failed",
        )
    finally:
        if not payload.user:
            await manager.clear_session(session_id=session_id, user_id=session_key)

    content = "\n\n".join(
        message.strip()
        for message in collected_messages
        if message and message.strip()
    ).strip()
    if not content and result:
        content = str(result).strip()
    if not content:
        content = "未获得有效回复。"

    created_at = int(time.time())
    response_id = f"resp_{uuid.uuid4().hex}"
    output_message = _SchemaOpenAIResponsesOutputMessage(
        id=f"msg_{uuid.uuid4().hex}",
        content=[_SchemaOpenAIResponsesOutputText(text=content)],
    )
    return _SchemaOpenAIResponsesResponse(
        id=response_id,
        created_at=created_at,
        model=MODEL_ID,
        output=[output_message],
        usage=_SchemaOpenAIUsage(),
    )
