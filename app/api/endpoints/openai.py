import asyncio
import json
import time
import uuid
from typing import AsyncIterator, List, Optional, Tuple

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from app import schemas
from app.api.openai_utils import (
    build_completion_payload,
    build_prompt,
    build_responses_input,
    build_session_id,
)
from app.agent import MoviePilotAgent, StreamingHandler
from app.core.config import settings
from app.core.security import openai_bearer_scheme
from app.schemas.types import MessageChannel

router = APIRouter()

MODEL_ID = "moviepilot-agent"
SESSION_PREFIX = "openai:"


class _CollectingMoviePilotAgent(MoviePilotAgent):
    """
    捕获 Agent 最终输出，避免再通过消息渠道二次发送。
    """

    def __init__(self, *args, stream_mode: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.collected_messages: List[str] = []
        self.stream_mode = stream_mode
        if stream_mode:
            self.stream_handler = _OpenAIStreamingHandler()

    def _should_stream(self) -> bool:
        return self.stream_mode

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

    async def _save_agent_message_to_db(self, message: str, title: str = ""):
        return None


class _OpenAIStreamingHandler(StreamingHandler):
    """
    将 Agent 流式输出转发到 OpenAI SSE 队列，不向站内消息系统落消息。
    """

    def __init__(self):
        super().__init__()
        self._event_queue: Optional[asyncio.Queue] = None

    def bind_queue(self, queue: asyncio.Queue):
        self._event_queue = queue

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


def _sse_payload(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_response(
    agent: _CollectingMoviePilotAgent,
    prompt: str,
    images: List[str],
) -> AsyncIterator[str]:
    event_queue: asyncio.Queue = asyncio.Queue()
    if isinstance(agent.stream_handler, _OpenAIStreamingHandler):
        agent.stream_handler.bind_queue(event_queue)

    created = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    finished = False

    async def _run_agent():
        try:
            await agent.process(prompt, images=images, files=None)
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
                raise RuntimeError(str(item["error"]))
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
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        elif finished:
            await task


def _error_response(
    message: str,
    status_code: int,
    error_type: str = "invalid_request_error",
    code: Optional[str] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=schemas.OpenAIErrorResponse(
            error=schemas.OpenAIErrorDetail(
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
    response_model=schemas.OpenAIModelListResponse,
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
    return schemas.OpenAIModelListResponse(
        data=[schemas.OpenAIModelInfo(id=MODEL_ID, created=now)]
    )


@router.post(
    "/chat/completions",
    summary="OpenAI compatible chat completions",
    response_model=schemas.OpenAIChatCompletionResponse,
)
async def chat_completions(
    payload: schemas.OpenAIChatCompletionsRequest,
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
    agent = _CollectingMoviePilotAgent(
        session_id=session_id,
        user_id=session_key,
        channel=MessageChannel.Web.value,
        source="openai",
        username=username,
        stream_mode=payload.stream,
    )

    if payload.stream:
        return StreamingResponse(
            _stream_response(agent=agent, prompt=prompt, images=images),
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
        return _error_response(
            str(exc),
            500,
            error_type="server_error",
            code="agent_execution_failed",
        )

    content = "\n\n".join(
        message.strip()
        for message in agent.collected_messages
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
    response_model=schemas.OpenAIResponsesResponse,
)
async def responses(
    payload: schemas.OpenAIResponsesRequest,
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
    agent = _CollectingMoviePilotAgent(
        session_id=session_id,
        user_id=session_key,
        channel=MessageChannel.Web.value,
        source="openai.responses",
        username=str(payload.user or "openai-client"),
        stream_mode=False,
    )

    try:
        result = await agent.process(prompt, images=images, files=None)
    except Exception as exc:
        return _error_response(
            str(exc),
            500,
            error_type="server_error",
            code="agent_execution_failed",
        )

    content = "\n\n".join(
        message.strip()
        for message in agent.collected_messages
        if message and message.strip()
    ).strip()
    if not content and result:
        content = str(result).strip()
    if not content:
        content = "未获得有效回复。"

    created_at = int(time.time())
    response_id = f"resp_{uuid.uuid4().hex}"
    output_message = schemas.OpenAIResponsesOutputMessage(
        id=f"msg_{uuid.uuid4().hex}",
        content=[schemas.OpenAIResponsesOutputText(text=content)],
    )
    return schemas.OpenAIResponsesResponse(
        id=response_id,
        created_at=created_at,
        model=MODEL_ID,
        output=[output_message],
        usage=schemas.OpenAIUsage(),
    )
