import asyncio
import copy
import hashlib
import json
import mimetypes
import shutil
import subprocess
import time
import uuid
from collections import deque
from queue import Empty, Queue
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterator, Callable, Optional, Union

import aiofiles
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse

from app.schemas.agent import AgentChatDisplaySaveRequest as _SchemaAgentChatDisplaySaveRequest
from app.schemas.agent import AgentChatSessionDetail as _SchemaAgentChatSessionDetail
from app.schemas.agent import AgentChatSessionSummary as _SchemaAgentChatSessionSummary
from app.schemas.agent import AgentChatUploadAttachment as _SchemaAgentChatUploadAttachment
from app.schemas.agent import AgentMcpServerListData as _SchemaAgentMcpServerListData
from app.schemas.agent import AgentMcpServerTestRequest as _SchemaAgentMcpServerTestRequest
from app.schemas.agent import AgentMcpServerTestResult as _SchemaAgentMcpServerTestResult
from app.schemas.agent import AgentMcpServersSaveRequest as _SchemaAgentMcpServersSaveRequest
from app.schemas.agent import AgentSessionStopData as _SchemaAgentSessionStopData
from app.schemas.agent import AgentWebCallbackData as _SchemaAgentWebCallbackData
from app.schemas.agent import AgentWebCommandInfo as _SchemaAgentWebCommandInfo
from app.schemas.message import AgentWebChatRequest as _SchemaAgentWebChatRequest
from app.schemas.message import AgentWebChoiceRequest as _SchemaAgentWebChoiceRequest
from app.schemas.message import Message as _SchemaMessage
from app.schemas.response import Response as _SchemaResponse
from app.api.response import ResponseAPIRouter
from app.api.presentation.sse import build_sse_error_response, build_sse_response
from app.agent.contracts import ReplyMode, build_display_message
from app.agent.llm.capability import AgentCapabilityManager
from app.agent.mcp import agent_mcp_manager
from app.agent.runtime_loader import (
    get_moviepilot_agent_type,
    get_running_agent_manager,
)
from app.application.orchestration.message import MessageChain
from app.runtime.command import Command
from app.runtime.config import global_vars
from app.runtime.events import Event, EventManager
from app.api.principal import ApiPrincipal
from app.api.deps import get_agent_chat_service, get_current_active_user
from app.application.messaging.chat import (
    AgentChatRecord,
    AgentChatService,
    get_configured_agent_chat_service,
)
from app.application.security.user import get_configured_user_id_lookup
from app.application.configuration import get_api_runtime_config_snapshot
from app.application.messaging.agent import attach_web_agent_edit_queue, detach_web_agent_edit_queue
from app.application.messaging.agent import agent_interaction_manager
from app.application.messaging.agent import (
    build_agent_choice_button_rows,
    normalize_web_agent_button_rows,
    parse_agent_choice_callback,
)
from app.application.messaging.router import has_pending_interaction
from app.runtime.localization import LocaleHelper
from app.runtime.log import logger
from app.schemas.notification import channel_identity
from app.schemas.types import EventType, NotificationChannel

router = ResponseAPIRouter()

WEB_AGENT_SESSION_PREFIX = "web-agent:"
WEB_AGENT_SOURCE = "web-agent"
WEB_AGENT_FILE_TTL_SECONDS = 6 * 60 * 60
WEB_AGENT_FILE_MAX_ITEMS = 256
WEB_AGENT_UPLOAD_MAX_BYTES = 32 * 1024 * 1024
WEB_AGENT_UPLOAD_CHUNK_SIZE = 1024 * 1024
WEB_AGENT_BROWSER_AUDIO_SUFFIXES = {".aac", ".m4a", ".mp3", ".mp4", ".wav", ".wave"}
WEB_AGENT_TRADITIONAL_IDLE_TIMEOUT_SECONDS = 2.0
WEB_AGENT_TRADITIONAL_MAX_WAIT_SECONDS = 60.0
WEB_AGENT_STREAM_COALESCE_SECONDS = 0.03
WEB_AGENT_STREAM_COALESCE_MAX_CHARS = 256
WEB_AGENT_STREAM_HEARTBEAT_SECONDS = 15.0
WEB_AGENT_STREAM_QUEUE_MAX_SIZE = 64
_WEB_AGENT_FILE_REGISTRY: dict[str, dict[str, Any]] = {}
_WEB_AGENT_MESSAGE_QUEUES: dict[str, list[Queue[_SchemaMessage]]] = {}
_WEB_AGENT_MESSAGE_LOCK = Lock()
_WEB_AGENT_MESSAGE_LISTENER_REGISTERED = False
_WEB_AGENT_BACKGROUND_TASKS: set[asyncio.Task] = set()


class _WebAgentEventPublisher:
    """合并 WebAgent 文本增量，并通过有界队列向 SSE 消费者提供事件。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue(
            maxsize=WEB_AGENT_STREAM_QUEUE_MAX_SIZE
        )
        self._pending_events: deque[dict] = deque()
        self._pending_signal = asyncio.Event()
        self._pending_delta = ""
        self._delta_timer: Optional[asyncio.TimerHandle] = None
        self._disposed = False
        self._max_depth = 0
        self._last_logged_depth = 0
        self._pump_task = asyncio.create_task(self._pump())

    @property
    def max_depth(self) -> int:
        """返回本轮发布器观测到的最大积压深度。"""
        return self._max_depth

    def publish(self, event: dict) -> bool:
        """发布事件；返回关闭状态以便受保护投递能准确报告失败。"""
        if self._disposed:
            return False
        if event.get("type") == "delta":
            self._pending_delta += str(event.get("content") or "")
            if len(self._pending_delta) >= WEB_AGENT_STREAM_COALESCE_MAX_CHARS:
                self._flush_delta()
            elif self._delta_timer is None:
                loop = asyncio.get_running_loop()
                self._delta_timer = loop.call_later(
                    WEB_AGENT_STREAM_COALESCE_SECONDS,
                    self._flush_delta,
                )
            return True

        self._flush_delta()
        self._append_event(event)
        return True

    async def get(self) -> dict:
        """等待并返回下一条已排序事件。"""
        return await self._queue.get()

    async def aclose(self) -> None:
        """停止发布器并释放等待中的泵任务。"""
        if self._disposed:
            return
        self._disposed = True
        self._cancel_delta_timer()
        self._pending_delta = ""
        self._pending_events.clear()
        self._pump_task.cancel()
        try:
            await self._pump_task
        except asyncio.CancelledError:
            pass

    def _cancel_delta_timer(self) -> None:
        """取消尚未触发的文本合并计时器。"""
        if self._delta_timer is None:
            return
        self._delta_timer.cancel()
        self._delta_timer = None

    def _flush_delta(self) -> None:
        """把当前文本缓冲转换成一条增量事件。"""
        self._cancel_delta_timer()
        if not self._pending_delta or self._disposed:
            return
        content = self._pending_delta
        self._pending_delta = ""
        self._append_event({"type": "delta", "content": content})

    def _append_event(self, event: dict) -> None:
        """追加待发布事件，相邻文本在出口阻塞时继续合并。"""
        if (
            event.get("type") == "delta"
            and self._pending_events
            and self._pending_events[-1].get("type") == "delta"
        ):
            self._pending_events[-1]["content"] += str(event.get("content") or "")
        else:
            self._pending_events.append(event)
        self._pending_signal.set()
        depth = self._queue.qsize() + len(self._pending_events)
        self._max_depth = max(self._max_depth, depth)
        if depth >= WEB_AGENT_STREAM_QUEUE_MAX_SIZE // 2 and depth > self._last_logged_depth:
            self._last_logged_depth = depth
            logger.debug(f"WebAgent SSE事件积压深度: {depth}")

    async def _pump(self) -> None:
        """按发布顺序把本地合并结果写入有界出口队列。"""
        while True:
            await self._pending_signal.wait()
            while self._pending_events:
                event = self._pending_events.popleft()
                await self._queue.put(event)
            self._pending_signal.clear()


def _ensure_superuser(user: ApiPrincipal) -> None:
    """校验当前用户是否为超级管理员。"""
    if not getattr(user, "is_superuser", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@router.get(
    "/mcp/servers",
    summary="查询 Agent MCP 服务器配置",
    response_model=_SchemaResponse[_SchemaAgentMcpServerListData],
)
async def list_agent_mcp_servers(
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    查询 Agent 外部 MCP 服务器配置。
    """
    _ensure_superuser(current_user)
    servers = agent_mcp_manager.get_servers()
    enabled_count = len([server for server in servers if server.enabled])
    return _SchemaResponse(
        success=True,
        data={
            "servers": [server.model_dump() for server in servers],
            "enabled_count": enabled_count,
            "total_count": len(servers),
        },
    )


@router.post(
    "/mcp/servers",
    summary="保存 Agent MCP 服务器配置",
    response_model=_SchemaResponse[None],
)
async def save_agent_mcp_servers(
    request: _SchemaAgentMcpServersSaveRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    保存 Agent 外部 MCP 服务器配置。
    """
    _ensure_superuser(current_user)
    success = await agent_mcp_manager.save_servers(request.servers)
    return _SchemaResponse(
        success=success,
        message="保存MCP配置成功" if success else "保存MCP配置失败",
    )


@router.post(
    "/mcp/servers/test",
    summary="测试 Agent MCP 服务器",
    response_model=_SchemaResponse[_SchemaAgentMcpServerTestResult],
)
async def test_agent_mcp_server(
    request: _SchemaAgentMcpServerTestRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    测试 Agent 外部 MCP 服务器连接并读取工具列表。
    """
    _ensure_superuser(current_user)
    try:
        result = await agent_mcp_manager.test_server(request.server)
        return _SchemaResponse(
            success=result.success,
            message=result.message,
            data=result.model_dump(),
        )
    except Exception as err:
        logger.warning(f"测试 Agent MCP 服务器失败: {err}")
        return _SchemaResponse(
            success=False,
            message=f"测试MCP服务器失败: {str(err)}",
            data={
                "success": False,
                "message": str(err),
                "tools": [],
                "tool_count": 0,
            },
        )


class _WebAgentStreamingHandlerMixin:
    """
    Web 前端专用流式处理器，将工具提示和文本统一回调给 SSE。
    """

    def __init__(self, on_emit: Callable[[str], None]) -> None:
        super().__init__()
        self._on_emit = on_emit

    def set_emit_callback(self, on_emit: Callable[[str], None]) -> None:
        """
        更新流式输出回调，复用 WebAgent 实例时指向当前 SSE 请求。

        :param on_emit: 当前请求的输出回调
        """
        self._on_emit = on_emit

    def record_tool_call(
        self,
        tool_name: str,
        tool_message: Optional[str] = None,
        tool_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        """记录并立即输出 Web 工具事件，避免汇总延迟到正文结束后。"""
        super().record_tool_call(
            tool_name=tool_name,
            tool_message=tool_message,
            tool_kwargs=tool_kwargs,
        )
        self.flush_pending_tool_summary()

    def emit(self, token: str) -> str:
        """追加 token 并同步通知 SSE 生产者。"""
        emitted = super().emit(token)
        if emitted:
            self._on_emit(emitted)
        return emitted

    def flush_pending_tool_summary(self) -> str:
        """输出延迟聚合的工具摘要。"""
        emitted = super().flush_pending_tool_summary()
        if emitted:
            self._on_emit(emitted)
        return ""

    async def start_streaming(
        self,
        channel: Optional[str] = None,
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        original_message_id: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        title: str = "",
    ) -> None:
        """Web SSE 自身负责外发，不启动消息模块编辑循环。"""
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
        self._pending_tool_stats = {}

    async def stop_streaming(self) -> tuple[bool, str]:
        """停止 Web SSE 流式状态，保留缓冲区给 Agent 收口逻辑去重。"""
        if not self._streaming_enabled:
            return False, ""
        self._streaming_enabled = False
        self.flush_pending_tool_summary()
        with self._lock:
            self._sent_text = ""
            self._message_response = None
            self._msg_start_offset = 0
            self._pending_tool_stats = {}
        return False, ""

    @property
    def is_auto_flushing(self) -> bool:
        """让工具执行提示进入缓冲区，由 SSE 回调负责外发。"""
        return True


def _get_web_agent_streaming_handler_type() -> type:
    """首次构造 Web Agent 时才解析完整流式处理器实现。"""
    global _WEB_AGENT_STREAMING_HANDLER_TYPE
    if _WEB_AGENT_STREAMING_HANDLER_TYPE is not None:
        return _WEB_AGENT_STREAMING_HANDLER_TYPE
    with _WEB_AGENT_STREAMING_HANDLER_TYPE_LOCK:
        if _WEB_AGENT_STREAMING_HANDLER_TYPE is None:
            from app.agent.callback import StreamingHandler

            _WEB_AGENT_STREAMING_HANDLER_TYPE = type(
                "_RuntimeWebAgentStreamingHandler",
                (_WebAgentStreamingHandlerMixin, StreamingHandler),
                {"__module__": __name__},
            )
        return _WEB_AGENT_STREAMING_HANDLER_TYPE


_WEB_AGENT_STREAMING_HANDLER_TYPE_LOCK = Lock()
_WEB_AGENT_STREAMING_HANDLER_TYPE: Optional[type] = None


class _WebAgentMoviePilotAgentMixin:
    """
    Web 前端专用 Agent，强制使用流式推理。
    """

    def __init__(
        self,
        *args: Any,
        message_callback: Optional[Callable[[_SchemaMessage], None]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._message_callback = message_callback
        self.stream_handler = _get_web_agent_streaming_handler_type()(
            self._emit_output
        )

    def _should_stream(self) -> bool:
        """Web 对话实时输出，复用会话执行后台任务时改用非流式广播。"""
        if self.is_background:
            return False
        return True

    def set_message_callback(
            self,
            message_callback: Optional[Callable[[_SchemaMessage], None]],
    ) -> None:
        """
        更新 Web SSE 通知回调，复用 Agent 实例时指向当前请求队列。

        :param message_callback: 当前请求的 Web 通知回调
        """
        self._message_callback = message_callback

    def set_output_callback(self, output_callback: Optional[Callable[[str], None]]) -> None:
        """
        更新 Web SSE 输出回调，复用 Agent 实例时指向当前请求队列。

        :param output_callback: 当前请求的输出回调
        """
        self.output_callback = output_callback
        if output_callback and isinstance(
            self.stream_handler, _WebAgentStreamingHandlerMixin
        ):
            self.stream_handler.set_emit_callback(self._emit_output)

    async def _is_system_admin_context(self) -> bool:
        """Web Agent 根据当前登录用户 ID 判断工具管理员上下文。"""
        if not self.user_id:
            return False
        try:
            user = get_configured_user_id_lookup()(int(self.user_id))
        except (TypeError, ValueError):
            return False
        except Exception as e:
            logger.error(f"检查 Web Agent 用户管理员身份失败: {e}")
            return False
        return bool(user and user.is_superuser)

    async def _build_tool_context(self, should_dispatch_reply: bool) -> dict[str, object]:
        """向工具上下文注入 Web SSE 通知回调。"""
        context = await super()._build_tool_context(should_dispatch_reply)
        context["message_callback"] = self._message_callback
        return context

    def _handle_stream_text(self, text: str) -> None:
        """文本输出交由 Web 流式处理器统一回调，避免重复增量。"""
        self.stream_handler.emit(text)

    def _emit_output(self, text: str) -> None:
        """保留完整输出状态，同时只把本次增量交给 Web SSE 回调。"""
        if not text:
            return
        self._streamed_output += text
        if not callable(self.output_callback):
            return
        try:
            self.output_callback(text)
        except Exception as e:
            logger.debug(f"Web智能体输出回调失败: {e}")


def _build_web_agent_type(agent_base_type: type) -> type:
    """为 Web 通道组合唯一的运行时 Agent 类型。"""
    return type(
        "_RuntimeWebAgentMoviePilotAgent",
        (_WebAgentMoviePilotAgentMixin, agent_base_type),
        {"__module__": __name__},
    )


_WEB_AGENT_TYPE_LOCK = Lock()
_WEB_AGENT_TYPE: Optional[type] = None


def _get_web_agent_type() -> type:
    """在真实 Web Agent 调用边界 single-flight 解析运行时类型。"""
    global _WEB_AGENT_TYPE
    if _WEB_AGENT_TYPE is not None:
        return _WEB_AGENT_TYPE
    with _WEB_AGENT_TYPE_LOCK:
        if _WEB_AGENT_TYPE is None:
            _WEB_AGENT_TYPE = _build_web_agent_type(get_moviepilot_agent_type())
        return _WEB_AGENT_TYPE


def _build_web_agent_session_id(user: ApiPrincipal, session_id: Optional[str]) -> str:
    """
    构建前端 Agent 会话 ID。

    :param user: 当前登录用户
    :param session_id: 前端传入的会话标识
    :return: 可用于 Agent 记忆隔离的服务端会话 ID
    """
    seed = str(session_id or "").strip() or uuid.uuid4().hex
    if seed.startswith(WEB_AGENT_SESSION_PREFIX):
        return seed
    try:
        existing_chat = get_configured_agent_chat_service().get_sync(seed)
        if existing_chat and AgentChatService.can_access(existing_chat, user):
            return seed
    except Exception as e:
        logger.debug(f"读取WebAgent历史会话失败: {e}")
    user_part = user.name or str(user.id)
    digest = hashlib.sha256(f"{user_part}:{seed}".encode("utf-8")).hexdigest()
    return f"{WEB_AGENT_SESSION_PREFIX}{digest[:32]}"


def _can_access_agent_chat(chat: Any, user: ApiPrincipal) -> bool:
    """
    判断当前登录用户是否可以访问指定 Agent 会话。

    超级用户可查看所有渠道历史；普通用户仅能查看 user_id 或 username 匹配自己的会话。
    """
    if not chat or not user:
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_id = str(user.id)
    username = str(user.name or "")
    return chat.user_id == user_id or (bool(username) and chat.username == username)


async def _get_accessible_agent_chat(
    service: AgentChatService,
    session_id: str,
    user: ApiPrincipal,
) -> Optional[AgentChatRecord]:
    """
    读取当前用户可访问的 Agent 会话。
    """
    return await service.get_accessible(session_id, user)


def _append_web_agent_text_segment(assistant_message: dict, content: str) -> None:
    """
    将文本增量追加到展示消息，并仅合并相邻文本片段。

    :param assistant_message: 当前助手展示消息
    :param content: 新增文本
    """
    if not content:
        return
    assistant_message["content"] = str(assistant_message.get("content") or "") + content
    segments = assistant_message.setdefault("segments", [])
    if segments and segments[-1].get("type") == "text":
        segments[-1]["content"] = str(segments[-1].get("content") or "") + content
    else:
        segments.append({"type": "text", "content": content})


def _build_legacy_web_agent_segments(content: str, tools: list[dict]) -> list[dict]:
    """
    为未携带有序片段的旧展示消息生成兼容布局。

    :param content: 聚合后的助手文本
    :param tools: 工具提示列表
    :return: 按旧版工具在前、文本在后的顺序生成的片段
    """
    segments = [
        {"type": "tool", "toolIndex": index}
        for index in range(len(tools))
    ]
    if content:
        segments.append({"type": "text", "content": content})
    return segments


def _apply_web_agent_display_event(event: dict, assistant_message: dict) -> None:
    """
    将 WebAgent SSE 事件同步应用到服务端展示消息快照。
    """
    event_type = event.get("type")
    if event_type == "delta":
        _append_web_agent_text_segment(
            assistant_message, event.get("content") or ""
        )
    elif event_type == "tool":
        for tool in assistant_message["tools"]:
            tool["status"] = "done"
        tool_index = len(assistant_message["tools"])
        assistant_message["tools"].append(
            {
                "id": f"tool-{uuid.uuid4().hex}",
                "message": str(event.get("message") or "").strip(),
                "status": "running",
            }
        )
        assistant_message.setdefault("segments", []).append(
            {"type": "tool", "toolIndex": tool_index}
        )
    elif event_type == "attachment" and event.get("attachment"):
        assistant_message["attachments"].append(event["attachment"])
    elif event_type == "choice" and event.get("choice"):
        assistant_message["choices"].append({**event["choice"], "status": "pending"})
    elif event_type == "message_update":
        target_message = event.get("target_message") or {}
        assistant_message["id"] = target_message.get("id") or assistant_message.get("id")
        assistant_message["content"] = target_message.get("content") or ""
        assistant_message["attachments"] = target_message.get("attachments") or []
        assistant_message["choices"] = target_message.get("choices") or []
        assistant_message["tools"] = target_message.get("tools") or []
        target_segments = target_message.get("segments")
        assistant_message["segments"] = (
            target_segments
            if isinstance(target_segments, list)
            else _build_legacy_web_agent_segments(
                assistant_message["content"], assistant_message["tools"]
            )
        )
        assistant_message["status"] = target_message.get("status") or "done"
    elif event_type == "error":
        assistant_message["status"] = "error"
        if not assistant_message["content"]:
            _append_web_agent_text_segment(
                assistant_message,
                event.get("message") or "智能助手响应失败",
            )
        for tool in assistant_message["tools"]:
            tool["status"] = "done"
    elif event_type == "done":
        if assistant_message.get("status") != "error":
            assistant_message["status"] = "done"
        for tool in assistant_message["tools"]:
            tool["status"] = "done"


def _save_web_agent_display_snapshot(
    *,
    session_id: str,
    current_user: ApiPrincipal,
    messages: list[dict],
    client_session_id: Optional[str] = None,
) -> None:
    """
    保存 WebAgent 当前展示消息快照。
    """
    try:
        service = get_configured_agent_chat_service()
        existing_chat = service.get_sync(session_id)
        service.save_display_sync(
            session_id=session_id,
            user_id=(existing_chat.user_id if existing_chat else str(current_user.id)),
            username=(existing_chat.username if existing_chat else current_user.name),
            channel=(
                existing_chat.channel
                if existing_chat and existing_chat.channel
                else NotificationChannel.WebAgent
            ),
            source=(
                existing_chat.source
                if existing_chat and existing_chat.source
                else WEB_AGENT_SOURCE
            ),
            original_chat_id=existing_chat.original_chat_id if existing_chat else None,
            client_session_id=(
                existing_chat.client_session_id
                if existing_chat and existing_chat.client_session_id
                else client_session_id
            ),
            messages=messages,
        )
    except Exception as e:
        logger.debug(f"保存WebAgent展示历史失败: {e}")


def _build_web_agent_sse(
        event_type: str,
        data: Optional[dict] = None,
        locale: Optional[str] = None,
) -> str:
    """
    构建 Web Agent SSE 消息。

    :param event_type: 前端事件类型
    :param data: 事件数据
    :param locale: 当前请求语言
    :return: 符合 SSE 格式的字符串
    """
    if event_type == "interaction-protected":
        return (
            "event: interaction-protected\n"
            f"data: {json.dumps(data or {}, ensure_ascii=False)}\n\n"
        )
    payload = {"type": event_type, **(data or {})}
    message = payload.get("message")
    if event_type == "error" and isinstance(message, str):
        payload["message_i18n"] = LocaleHelper.translate_text(
            message, locale=locale
        )
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_web_agent_error_response(
    message: str,
    *,
    locale: Optional[str],
) -> StreamingResponse:
    """Map a rejected WebAgent request to one terminal SSE error event."""
    return build_sse_error_response(
        _build_web_agent_sse(
            "error",
            {"message": message},
            locale=locale,
        )
    )


def _sanitize_web_agent_upload_name(
    filename: Optional[str], mime_type: Optional[str] = None
) -> str:
    """
    规范化 Web Agent 上传文件名，避免路径穿越和空文件名。

    :param filename: 浏览器上传的原始文件名
    :param mime_type: 浏览器上报的 MIME 类型
    :return: 可安全落盘的文件名
    """
    name = Path(filename or "attachment").name.strip()
    safe_name = "".join(
        char for char in name if char.isalnum() or char in (" ", ".", "_", "-")
    ).strip(" .")
    if not safe_name:
        safe_name = "attachment"
    if "." not in safe_name:
        suffix = mimetypes.guess_extension(mime_type or "") or ""
        safe_name = f"{safe_name}{suffix}"
    return safe_name


def _get_web_agent_upload_dir(user: ApiPrincipal, session_id: Optional[str]) -> Path:
    """
    计算当前 Web Agent 会话的临时附件目录。

    :param user: 当前登录用户
    :param session_id: 前端会话标识
    :return: 已创建的临时附件目录
    """
    server_session_id = _build_web_agent_session_id(user, session_id)
    safe_session_id = server_session_id.replace(":", "_")
    upload_dir = get_api_runtime_config_snapshot().temp_path / "agent_uploads" / safe_session_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


async def _save_web_agent_upload(upload_file: UploadFile, target_path: Path) -> int:
    """
    分块保存 Web Agent 上传文件，并限制单文件体积。

    :param upload_file: FastAPI 上传文件对象
    :param target_path: 目标落盘路径
    :return: 已写入的字节数
    """
    size = 0
    try:
        async with aiofiles.open(target_path, "wb") as output:
            while True:
                chunk = await upload_file.read(WEB_AGENT_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > WEB_AGENT_UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="附件超过 32MB，无法发送给智能助手",
                    )
                await output.write(chunk)
    except Exception:
        await run_in_threadpool(target_path.unlink, missing_ok=True)
        raise
    finally:
        await upload_file.close()
    return size


def _cleanup_web_agent_file_registry() -> None:
    """清理过期或过量的 Web Agent 临时附件引用。"""
    now = time.time()
    expired_ids = [
        file_id
        for file_id, info in _WEB_AGENT_FILE_REGISTRY.items()
        if now - info.get("created_at", now) > WEB_AGENT_FILE_TTL_SECONDS
    ]
    for file_id in expired_ids:
        _WEB_AGENT_FILE_REGISTRY.pop(file_id, None)

    overflow = len(_WEB_AGENT_FILE_REGISTRY) - WEB_AGENT_FILE_MAX_ITEMS
    if overflow <= 0:
        return
    sorted_items = sorted(
        _WEB_AGENT_FILE_REGISTRY.items(),
        key=lambda item: item[1].get("created_at", 0),
    )
    for file_id, _ in sorted_items[:overflow]:
        _WEB_AGENT_FILE_REGISTRY.pop(file_id, None)


def _guess_web_agent_attachment_kind(
    mime_type: Optional[str], fallback: str = "file"
) -> str:
    """
    根据 MIME 类型推断前端附件展示方式。

    :param mime_type: 文件 MIME 类型
    :param fallback: 无法推断时使用的类型
    :return: image、audio 或 file
    """
    if mime_type and mime_type.startswith("image/"):
        return "image"
    if mime_type and mime_type.startswith("audio/"):
        return "audio"
    return fallback


def _build_web_agent_url_attachment(
    url: str,
    kind: str,
    name: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> dict:
    """
    构建远程或 data URL 附件事件。

    :param url: 前端可访问的附件地址
    :param kind: 附件展示类型
    :param name: 展示名称
    :param mime_type: MIME 类型
    :return: 前端附件描述
    """
    return {
        "kind": kind,
        "url": url,
        "download_url": url,
        "name": name,
        "mime_type": mime_type,
    }


def _build_web_agent_input_attachments(
    images: list[str],
    files: list[dict],
    audio_refs: list[str],
) -> list[dict]:
    """
    构造 WebAgent 用户输入附件展示记录。
    """
    attachments = []
    for index, image in enumerate(images or [], start=1):
        attachments.append(
            {
                "kind": "image",
                "url": image,
                "download_url": image,
                "name": f"image-{index}",
                "mime_type": "image/*",
            }
        )
    for index, file in enumerate(files or [], start=1):
        ref = file.get("ref") or file.get("url") or file.get("local_path") or ""
        mime_type = file.get("mime_type")
        attachments.append(
            {
                "kind": _guess_web_agent_attachment_kind(mime_type),
                "url": ref,
                "download_url": ref,
                "name": file.get("name") or f"attachment-{index}",
                "mime_type": mime_type,
                "size": file.get("size"),
                "local_path": file.get("local_path"),
            }
        )
    for index, audio_ref in enumerate(audio_refs or [], start=1):
        attachments.append(
            {
                "kind": "audio",
                "url": audio_ref,
                "download_url": audio_ref,
                "name": f"voice-{index}",
                "mime_type": "audio/*",
            }
        )
    return attachments


def _register_web_agent_file(
    file_path: Optional[str],
    file_name: Optional[str] = None,
    kind: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Optional[dict]:
    """
    注册 Web Agent 本地附件并返回前端可访问的短期下载地址。

    :param file_path: 本地文件路径
    :param file_name: 前端展示文件名
    :param kind: 附件展示类型
    :param mime_type: 已知 MIME 类型
    :return: 前端附件描述，文件不可访问时返回 None
    """
    if not file_path:
        return None
    try:
        resolved_path = Path(file_path).expanduser().resolve(strict=True)
    except OSError:
        return None
    if not resolved_path.is_file():
        return None

    _cleanup_web_agent_file_registry()
    file_id = uuid.uuid4().hex
    display_name = file_name or resolved_path.name
    resolved_mime_type = mime_type or mimetypes.guess_type(
        display_name or str(resolved_path)
    )[0]
    file_url = f"message/agent/file/{file_id}"
    _WEB_AGENT_FILE_REGISTRY[file_id] = {
        "path": resolved_path,
        "name": display_name,
        "mime_type": resolved_mime_type or "application/octet-stream",
        "created_at": time.time(),
    }
    return {
        "kind": kind or _guess_web_agent_attachment_kind(resolved_mime_type),
        "url": file_url,
        "download_url": file_url,
        "name": display_name,
        "mime_type": resolved_mime_type,
        "size": resolved_path.stat().st_size,
    }


def _get_web_agent_audio_mime_type(audio_path: Path) -> Optional[str]:
    """
    生成浏览器播放更友好的音频 MIME 类型。

    :param audio_path: 音频文件路径
    :return: 可用于 FileResponse/audio 标签的 MIME 类型
    """
    suffix = audio_path.suffix.lower()
    if suffix in {".wav", ".wave"}:
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix in {".m4a", ".mp4"}:
        return "audio/mp4"
    if suffix == ".aac":
        return "audio/aac"

    return mimetypes.guess_type(audio_path.name)[0]


def _prepare_web_agent_audio_attachment_path(voice_path: str) -> Path:
    """
    将 Agent 语音回复准备成 Web 面板可稳定播放的音频文件。

    部分 TTS provider 会生成 Opus/Ogg，桌面 Chromium 通常可播放，但 iOS/Safari
    兼容性不稳定；WebAgent 只在浏览器内播放，因此这里单独转成 WAV。
    """
    try:
        source_path = Path(voice_path).expanduser().resolve(strict=True)
    except OSError:
        return Path(voice_path)
    if source_path.suffix.lower() in WEB_AGENT_BROWSER_AUDIO_SUFFIXES:
        return source_path
    if not shutil.which("ffmpeg"):
        logger.warning("WebAgent 语音转 WAV 跳过：ffmpeg 不可用，path=%s", source_path)
        return source_path

    voice_dir = get_api_runtime_config_snapshot().temp_path / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    output_path = voice_dir / f"{source_path.stem}_web_{uuid.uuid4().hex[:8]}.wav"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-ar",
        "24000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not output_path.exists():
        logger.warning(
            "WebAgent 语音转 WAV 失败，将回退原文件: returncode=%s, stderr=%s",
            result.returncode,
            (result.stderr or "").strip()[:500],
        )
        return source_path
    return output_path


def _get_web_agent_registered_file(ref: str) -> Optional[dict[str, Any]]:
    """
    根据前端附件引用读取 WebAgent 临时文件登记信息。

    :param ref: message/agent/file/{file_id} 形式的短期引用
    :return: 文件登记信息，引用无效或过期时返回 None
    """
    normalized_ref = (ref or "").strip()
    prefix = "message/agent/file/"
    if not normalized_ref.startswith(prefix):
        return None

    _cleanup_web_agent_file_registry()
    file_id = normalized_ref[len(prefix):].split("/", 1)[0]
    return _WEB_AGENT_FILE_REGISTRY.get(file_id)


def _resolve_web_agent_audio_refs(
    audio_refs: list[str],
) -> list[tuple[str, Path, str]]:
    """在调用协程中解析音频引用，返回不依赖登记表的文件快照。"""
    audio_files = []
    for audio_ref in audio_refs:
        file_info = _get_web_agent_registered_file(audio_ref)
        if not file_info:
            logger.warning("WebAgent 语音引用不存在或已过期: ref=%s", audio_ref)
            continue

        file_path = Path(file_info["path"])
        audio_files.append(
            (audio_ref, file_path, file_info.get("name") or file_path.name)
        )
    return audio_files


def _transcribe_web_agent_audio_files(
    audio_files: list[tuple[str, Path, str]],
) -> Optional[str]:
    """
    转写 WebAgent 上传的本地录音附件。

    文件信息已在调用协程中从短期登记表解析，阻塞文件读取和 provider 调用可在
    worker 中执行，避免跨线程访问登记表。
    """
    if not audio_files:
        return None
    if not AgentCapabilityManager.is_audio_input_available():
        logger.warning("WebAgent 音频输入能力未配置或未启用，跳过语音识别")
        return None

    transcripts = []
    for audio_ref, file_path, file_name in audio_files:
        try:
            content = file_path.read_bytes()
        except OSError as err:
            logger.warning("WebAgent 语音文件读取失败: ref=%s, error=%s", audio_ref, err)
            continue

        transcript = AgentCapabilityManager.transcribe_audio(
            content=content,
            filename=file_name,
        )
        if transcript:
            transcripts.append(transcript)

    return "\n".join(transcripts).strip() if transcripts else None


async def _transcribe_web_agent_audio_input(audio_refs: list[str]) -> Optional[str]:
    """解析并在线程池中转写 WebAgent 音频引用。"""
    audio_files = _resolve_web_agent_audio_refs(audio_refs)
    if not audio_files:
        return None
    return await asyncio.to_thread(_transcribe_web_agent_audio_files, audio_files)


def _merge_web_agent_prompt_with_transcript(prompt: str, transcript: Optional[str]) -> str:
    """合并用户输入文本和语音转写文本，避免重复发送相同内容。"""
    merged_parts = []
    seen_parts = set()
    for item in (prompt, transcript or ""):
        normalized = item.strip()
        if not normalized or normalized in seen_parts:
            continue
        seen_parts.add(normalized)
        merged_parts.append(normalized)
    return "\n".join(merged_parts).strip()


def _build_web_agent_choice_event(message: _SchemaMessage) -> Optional[dict]:
    """
    将带按钮通知转换为 Web Agent 选择卡片事件。

    :param message: Agent 工具发出的按钮通知
    :return: 选择卡片事件，按钮为空时返回 None
    """
    button_rows = normalize_web_agent_button_rows(message.buttons)
    buttons = [button for row in button_rows for button in row]
    if not buttons:
        return None

    choice_id = None
    parsed = parse_agent_choice_callback(buttons[0]["callback_data"])
    if parsed:
        choice_id = parsed[0]

    return {
        "type": "choice",
        "choice": {
            "id": choice_id or uuid.uuid4().hex,
            "title": message.title,
            "prompt": message.text or "",
            "buttons": buttons,
            "button_rows": button_rows,
        },
    }


def _resolve_web_agent_choice_payload(callback_data: str, user_id: str) -> Optional[dict]:
    """
    解析并消费 Web Agent 按钮选择，生成前端反馈与下一条用户消息。

    :param callback_data: 前端点击的按钮回调数据
    :param user_id: 当前登录用户 ID
    :return: 可返回给前端的数据，选择无效时返回 None
    """
    parsed = parse_agent_choice_callback(callback_data)
    if not parsed:
        return None

    request_id, option_index = parsed
    resolved = agent_interaction_manager.resolve(
        request_id=request_id,
        option_index=option_index,
        user_id=str(user_id),
    )
    if not resolved:
        return None

    request, option = resolved
    buttons, button_rows = build_agent_choice_button_rows(request)
    selected_description = option.description or option.label
    return {
        "message": option.value,
        "session_id": request.session_id,
        "display_message": selected_description,
        "choice_selection": {
            "choice_id": request.request_id,
            "title": request.title,
            "prompt": request.prompt,
            "buttons": buttons,
            "button_rows": button_rows,
            "selected_label": option.label,
            "selected_value": option.value,
            "selected_description": selected_description,
        },
        "feedback": {
            "request_id": request.request_id,
            "title": request.title,
            "prompt": request.prompt,
            "selected_label": option.label,
            "selected_value": option.value,
            "selected_description": selected_description,
            "buttons": buttons,
            "button_rows": button_rows,
        },
    }


def _build_web_agent_message_events(
    message: _SchemaMessage,
) -> list[dict]:
    """
    将 Agent 工具通知转换为 Web SSE 事件。

    :param message: 工具产生的通知消息
    :return: 前端可直接应用到当前助手消息的事件列表
    """
    events = []
    choice_event = _build_web_agent_choice_event(message)
    if choice_event:
        events.append(choice_event)

    text_parts = [
        str(item).strip()
        for item in (message.title, message.text)
        if str(item or "").strip()
    ]
    if text_parts and not choice_event:
        events.append({"type": "delta", "content": "\n\n".join(text_parts)})

    if message.image:
        image_ref = message.image
        image_path = Path(image_ref).expanduser()
        attachment = None
        if not image_ref.startswith(("http://", "https://", "data:", "blob:")):
            attachment = _register_web_agent_file(
                image_ref, file_name=Path(image_ref).name, kind="image"
            )
        if not attachment:
            attachment = _build_web_agent_url_attachment(
                image_ref,
                kind="image",
                name=message.title or image_path.name or "image",
            )
        events.append({"type": "attachment", "attachment": attachment})

    if message.voice_path:
        audio_path = _prepare_web_agent_audio_attachment_path(message.voice_path)
        attachment = _register_web_agent_file(
            str(audio_path),
            file_name=audio_path.name,
            kind="audio",
            mime_type=_get_web_agent_audio_mime_type(audio_path),
        )
        if attachment:
            events.append({"type": "attachment", "attachment": attachment})

    if message.file_path:
        attachment = _register_web_agent_file(
            message.file_path,
            file_name=message.file_name or Path(message.file_path).name,
        )
        if attachment:
            events.append({"type": "attachment", "attachment": attachment})

    return events


def _build_web_agent_display_message_from_events(
    events: list[dict],
) -> dict:
    """
    将传统消息事件聚合为前端展示消息快照。

    :param events: 已转换的 WebAgent SSE 事件列表
    :return: 可持久化的助手展示消息
    """
    message = build_display_message(
        role="assistant",
        status="streaming",
    )
    for event in events:
        _apply_web_agent_display_event(copy.deepcopy(event), message)
    _apply_web_agent_display_event({"type": "done"}, message)
    return message


def _is_web_agent_traditional_message(text: str) -> bool:
    """
    判断用户输入是否应走传统消息命令/交互链路。

    :param text: 前端输入文本
    :return: 需要交给 MessageChain 时返回 True
    """
    normalized = str(text or "").strip()
    return normalized.startswith("/") or normalized.startswith("CALLBACK:")


def _has_web_agent_traditional_interaction(user_id: str) -> bool:
    """
    判断当前用户是否存在待继续的传统交互会话。

    :param user_id: 当前登录用户 ID
    :return: 存在传统交互上下文时返回 True
    """
    return has_pending_interaction(user_id)


def _extract_web_agent_message_from_event_data(
    data: dict,
) -> Optional[_SchemaMessage]:
    """
    从 NoticeMessage 事件数据中提取 WebAgent 通知。

    :param data: NoticeMessage 事件数据，兼容扁平字段和 message 包装格式
    :return: WebAgent 通知，不属于 WebAgent 或数据无效时返回 None
    """
    if not isinstance(data, dict):
        return None

    try:
        message = data.get("message")
        if isinstance(message, _SchemaMessage):
            message = message
        elif isinstance(message, dict):
            message_data = copy.deepcopy(message)
            message_data.pop("type", None)
            message = _SchemaMessage(**message_data)
        else:
            message_data = copy.deepcopy(data)
            message_data.pop("type", None)
            message_data.pop("current_time", None)
            message = _SchemaMessage(**message_data)
    except Exception as err:
        logger.debug(f"解析WebAgent通知事件失败: {err}")
        return None

    if channel_identity(message.channel) != NotificationChannel.WebAgent.value:
        return None
    return message


def _is_web_agent_message_for_user(
    message: _SchemaMessage,
    user_id: str,
) -> bool:
    """
    判断 NoticeMessage 事件是否属于当前 WebAgent 用户。

    :param message: NoticeMessage 中的通知消息
    :param user_id: 当前登录用户 ID
    :return: 可被本次 WebAgent 请求消费时返回 True
    """
    try:
        target_user = message.userid
        return target_user is None or str(target_user) == str(user_id)
    except Exception:
        return False


def _get_web_agent_message_user_id(message: _SchemaMessage) -> Optional[str]:
    """
    从 NoticeMessage 事件中解析 WebAgent 目标用户。

    :param message: NoticeMessage 中的通知消息
    :return: 用户 ID 字符串，事件不属于 WebAgent 时返回 None
    """
    try:
        if channel_identity(message.channel) != NotificationChannel.WebAgent.value:
            return None
        user_id = message.userid
        return str(user_id) if user_id is not None else None
    except Exception:
        return None


def _dispatch_web_agent_message_event(event: Event) -> None:
    """
    将 WebAgent NoticeMessage 分发给正在等待的请求队列。

    :param event: NoticeMessage 广播事件
    """
    data = event.event_data if isinstance(event.event_data, dict) else {}
    message = _extract_web_agent_message_from_event_data(data)
    if not message:
        return
    with _WEB_AGENT_MESSAGE_LOCK:
        user_id = _get_web_agent_message_user_id(message)
        if user_id is None:
            queues = [
                message_queue
                for user_queues in _WEB_AGENT_MESSAGE_QUEUES.values()
                for message_queue in user_queues
            ]
        else:
            queues = list(_WEB_AGENT_MESSAGE_QUEUES.get(user_id) or [])
    for message_queue in queues:
        message_queue.put(message)


def _ensure_web_agent_message_listener() -> None:
    """
    确保 WebAgent NoticeMessage 全局监听器已注册。
    """
    global _WEB_AGENT_MESSAGE_LISTENER_REGISTERED
    if _WEB_AGENT_MESSAGE_LISTENER_REGISTERED:
        return
    with _WEB_AGENT_MESSAGE_LOCK:
        if _WEB_AGENT_MESSAGE_LISTENER_REGISTERED:
            return
        EventManager().add_event_listener(
            EventType.NoticeMessage,
            _dispatch_web_agent_message_event,
        )
        _WEB_AGENT_MESSAGE_LISTENER_REGISTERED = True


def _attach_web_agent_message_queue(user_id: str, message_queue: Queue[_SchemaMessage]) -> None:
    """
    为当前 WebAgent 请求挂载通知收集队列。

    :param user_id: 当前用户 ID
    :param message_queue: 用于接收通知事件的队列
    """
    _ensure_web_agent_message_listener()
    with _WEB_AGENT_MESSAGE_LOCK:
        _WEB_AGENT_MESSAGE_QUEUES.setdefault(str(user_id), []).append(message_queue)


def _detach_web_agent_message_queue(user_id: str, message_queue: Queue[_SchemaMessage]) -> None:
    """
    移除当前 WebAgent 请求的通知收集队列。

    :param user_id: 当前用户 ID
    :param message_queue: 需要移除的队列
    """
    with _WEB_AGENT_MESSAGE_LOCK:
        queues = _WEB_AGENT_MESSAGE_QUEUES.get(str(user_id))
        if not queues:
            return
        _WEB_AGENT_MESSAGE_QUEUES[str(user_id)] = [
            item for item in queues if item is not message_queue
        ]
        if not _WEB_AGENT_MESSAGE_QUEUES[str(user_id)]:
            _WEB_AGENT_MESSAGE_QUEUES.pop(str(user_id), None)


def _build_web_agent_command_items() -> list[dict]:
    """
    读取当前可用斜杠命令并转换为前端建议列表。

    :return: 按分类和命令名排序的命令列表
    """
    commands = Command().get_commands() or {}
    items = []
    for command, data in commands.items():
        if not command.startswith("/"):
            continue
        if data.get("show") is False:
            continue
        items.append(
            {
                "command": command,
                "description": data.get("description") or "",
                "category": data.get("category") or "其他",
                "type": data.get("type") or "",
                "pid": data.get("pid"),
            }
        )
    return sorted(items, key=lambda item: (item["category"], item["command"]))


def _extract_web_agent_slash_command(text: str) -> Optional[str]:
    """
    从 WebAgent 输入中提取斜杠命令名。

    :param text: 前端输入文本
    :return: 斜杠命令名，非命令输入返回 None
    """
    normalized = str(text or "").strip()
    if not normalized.startswith("/") or normalized.startswith("//"):
        return None
    command = normalized.split(maxsplit=1)[0].strip()
    return command or None


def _get_web_agent_unknown_command_message(text: str) -> Optional[str]:
    """
    判断 WebAgent 斜杠命令是否不存在。

    :param text: 前端输入文本
    :return: 命令不存在时返回错误提示，命令存在或非命令时返回 None
    """
    command = _extract_web_agent_slash_command(text)
    if not command:
        return None
    if Command().get(command):
        return None
    return f"命令不存在：{command}"


def _ensure_web_agent_command_allowed(current_user: ApiPrincipal) -> Optional[str]:
    """
    校验当前 Web 用户是否可以执行传统斜杠命令。

    :param current_user: 当前登录用户
    :return: 无权限时返回错误提示，允许执行时返回 None
    """
    if getattr(current_user, "is_superuser", False):
        return None
    return "只有管理员才有权限执行此命令"


async def _collect_web_agent_traditional_events(
    *,
    text: str,
    current_user: ApiPrincipal,
    original_message_id: Optional[Union[str, int]] = None,
    original_chat_id: Optional[Union[str, int]] = None,
) -> list[dict]:
    """
    执行传统消息链路并收集本次 WebAgent 用户产生的通知事件。

    :param text: 需要交给传统消息链路处理的文本
    :param current_user: 当前登录用户
    :param original_message_id: WebAgent 原助手消息 ID
    :param original_chat_id: WebAgent 原聊天 ID
    :return: 可直接发送给前端的 SSE 事件列表
    """
    message_queue: Queue[_SchemaMessage] = Queue()
    edit_queue: Queue[dict] = Queue()
    user_id = str(current_user.id)

    _attach_web_agent_message_queue(user_id, message_queue)
    attach_web_agent_edit_queue(user_id, edit_queue)
    try:
        await run_in_threadpool(
            MessageChain().handle_message,
            channel=NotificationChannel.WebAgent,
            source=WEB_AGENT_SOURCE,
            userid=user_id,
            username=current_user.name or user_id,
            text=text,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

        events = []
        deadline = time.monotonic() + WEB_AGENT_TRADITIONAL_MAX_WAIT_SECONDS
        idle_deadline: Optional[float] = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            drained_edit_event = False
            while True:
                try:
                    events.append(edit_queue.get_nowait())
                    drained_edit_event = True
                except Empty:
                    break
            if drained_edit_event:
                idle_deadline = time.monotonic() + WEB_AGENT_TRADITIONAL_IDLE_TIMEOUT_SECONDS
                continue

            wait_until = idle_deadline or deadline
            timeout = max(0.05, min(0.25, wait_until - now, deadline - now))
            try:
                message = await asyncio.to_thread(message_queue.get, True, timeout)
            except Empty:
                if idle_deadline and time.monotonic() >= idle_deadline:
                    break
                continue

            if not _is_web_agent_message_for_user(message, user_id):
                continue
            events.extend(_build_web_agent_message_events(message))
            idle_deadline = time.monotonic() + WEB_AGENT_TRADITIONAL_IDLE_TIMEOUT_SECONDS
        return events
    finally:
        _detach_web_agent_message_queue(user_id, message_queue)
        detach_web_agent_edit_queue(user_id, edit_queue)


def _build_web_agent_traditional_callback_payload(
    callback_data: str,
    original_message_id: Optional[Union[str, int]] = None,
    original_chat_id: Optional[Union[str, int]] = None,
) -> dict:
    """
    构造传统消息链按钮回调的前端执行载荷。

    :param callback_data: 前端点击的传统按钮回调数据
    :param original_message_id: WebAgent 原助手消息 ID
    :param original_chat_id: WebAgent 原聊天 ID
    :return: 前端可继续发送到 /stream 的消息载荷
    """
    return {
        "message": f"CALLBACK:{callback_data}",
        "display_message": callback_data,
        "traditional": True,
        "original_message_id": original_message_id,
        "original_chat_id": original_chat_id,
    }


def _split_web_agent_output(text: str) -> list[dict]:
    """
    将 Agent 输出拆成普通文本与工具提示事件。

    :param text: 本次新增的 Agent 文本
    :return: 前端可直接渲染的事件片段
    """
    if not text:
        return []

    events = []

    def append_text(content: str) -> None:
        """将工具汇总行从普通文本中拆出，保留与消息渠道一致的展示文案。"""
        if not content:
            return
        lines = content.splitlines(keepends=True)
        buffer = ""
        for line in lines:
            stripped_line = line.strip()
            if stripped_line.startswith("（") and stripped_line.endswith("）"):
                if buffer:
                    events.append({"type": "delta", "content": buffer})
                    buffer = ""
                events.append(
                    {
                        "type": "tool",
                        "message": stripped_line,
                    }
                )
            else:
                buffer += line
        if buffer:
            events.append({"type": "delta", "content": buffer})

    marker = "⚙️ => "
    remaining = text
    while remaining:
        marker_index = remaining.find(marker)
        if marker_index < 0:
            append_text(remaining)
            break

        if marker_index > 0:
            append_text(remaining[:marker_index])

        after_marker = remaining[marker_index + len(marker):]
        line_end = after_marker.find("\n")
        if line_end < 0:
            message = after_marker.strip()
            remaining = ""
        else:
            message = after_marker[:line_end].strip()
            remaining = after_marker[line_end:].lstrip("\n")

        if message:
            events.append({"type": "tool", "message": f"{marker}{message}"})

    return events


@router.get(
    "/file/{file_id}",
    summary="下载 Web 智能助手附件",
    response_model=None,
    response_class=FileResponse,
    responses={
        200: {
            "description": "Agent 附件文件",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def download_web_agent_file(file_id: str) -> FileResponse:
    """
    下载 Web 智能助手本轮生成的临时附件。

    :param file_id: 附件随机标识
    :return: 附件文件响应
    """
    _cleanup_web_agent_file_registry()
    file_info = _WEB_AGENT_FILE_REGISTRY.get(file_id)
    if not file_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在或已过期")

    file_path = file_info["path"]
    if not file_path.exists() or not file_path.is_file():
        _WEB_AGENT_FILE_REGISTRY.pop(file_id, None)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在或已过期")

    return FileResponse(
        path=file_path,
        media_type=file_info.get("mime_type") or "application/octet-stream",
        filename=file_info.get("name") or file_path.name,
    )


@router.post(
    "/upload",
    summary="上传 Web 智能助手附件",
    response_model=_SchemaResponse[_SchemaAgentChatUploadAttachment],
)
async def upload_web_agent_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    上传 Web 智能助手对话附件。

    :param file: 浏览器选择的文件
    :param session_id: 前端会话标识
    :param current_user: 当前登录用户
    :return: Agent 可消费的附件描述
    """
    mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0]
    safe_name = _sanitize_web_agent_upload_name(file.filename, mime_type)
    upload_dir = _get_web_agent_upload_dir(current_user, session_id)
    target_path = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    size = await _save_web_agent_upload(file, target_path)
    attachment = _register_web_agent_file(
        str(target_path),
        file_name=safe_name,
        kind=_guess_web_agent_attachment_kind(mime_type),
        mime_type=mime_type,
    )
    if not attachment:
        target_path.unlink(missing_ok=True)
        return _SchemaResponse(success=False, message="附件保存失败")

    attachment.update(
        {
            "ref": attachment["url"],
            "local_path": str(target_path),
            "status": "ready",
            "size": size,
        }
    )
    return _SchemaResponse(success=True, data=attachment)


@router.post(
    "/callback",
    summary="Web 智能助手按钮回调",
    response_model=_SchemaResponse[_SchemaAgentWebCallbackData],
)
async def web_agent_callback(
    payload: _SchemaAgentWebChoiceRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    接收 Web 智能助手选择卡片回调。

    :param payload: 按钮选择请求
    :param current_user: 当前登录用户
    :return: 下一条需要发送给 Agent 的用户消息与卡片反馈
    """
    if not parse_agent_choice_callback(payload.callback_data):
        denied_message = _ensure_web_agent_command_allowed(current_user)
        if denied_message:
            return _SchemaResponse(success=False, message=denied_message)
        return _SchemaResponse(
            success=True,
            data=_build_web_agent_traditional_callback_payload(
                payload.callback_data,
                original_message_id=payload.original_message_id,
                original_chat_id=payload.original_chat_id,
            ),
        )

    result = _resolve_web_agent_choice_payload(
        callback_data=payload.callback_data,
        user_id=str(current_user.id),
    )
    if not result:
        return _SchemaResponse(success=False, message="该选择已失效，请重新发起选择")
    return _SchemaResponse(success=True, data=result)


@router.get(
    "/commands",
    summary="获取 Web 智能助手可用命令",
    response_model=_SchemaResponse[list[_SchemaAgentWebCommandInfo]],
)
async def list_web_agent_commands(
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    获取当前 Web 智能助手可补全的斜杠命令。

    :param current_user: 当前登录用户
    :return: 可用命令列表
    """
    denied_message = _ensure_web_agent_command_allowed(current_user)
    if denied_message:
        return _SchemaResponse(success=False, message=denied_message)
    return _SchemaResponse(success=True, data=_build_web_agent_command_items())


@router.get(
    "/sessions",
    summary="获取 Agent 历史会话",
    response_model=_SchemaResponse[list[_SchemaAgentChatSessionSummary]],
)
async def list_agent_chat_sessions(
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
    page: Optional[int] = 1,
    count: Optional[int] = 30,
) -> _SchemaResponse:
    """
    获取当前用户可访问的 Agent 历史会话列表。

    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :param page: 页码
    :param count: 每页数量
    :return: 会话摘要列表
    """
    chats = await service.list(
        current_user,
        page=page,
        count=count,
    )
    return _SchemaResponse(success=True, data=chats)


@router.get(
    "/sessions/{session_id}",
    summary="获取 Agent 历史会话详情",
    response_model=_SchemaResponse[_SchemaAgentChatSessionDetail],
)
async def get_agent_chat_session(
    session_id: str,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
) -> _SchemaResponse:
    """
    获取一条 Agent 历史会话详情。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :return: 会话详情
    """
    chat = await _get_accessible_agent_chat(service, session_id, current_user)
    server_session_id = session_id
    if not chat:
        server_session_id = _build_web_agent_session_id(current_user, session_id)
        if server_session_id != session_id:
            chat = await _get_accessible_agent_chat(
                service,
                server_session_id,
                current_user,
            )
    if not chat:
        manager = get_running_agent_manager()
        if manager and manager.is_session_busy(server_session_id):
            return _SchemaResponse(
                success=True,
                data={
                    "session_id": server_session_id,
                    "client_session_id": session_id,
                    "messages": [],
                    "is_processing": True,
                },
            )
        return _SchemaResponse(success=False, message="会话不存在或无权访问")
    data = service.to_detail(chat).model_dump()
    manager = get_running_agent_manager()
    data["is_processing"] = bool(
        manager and manager.is_session_busy(chat.session_id)
    )
    return _SchemaResponse(success=True, data=data)


@router.put(
    "/sessions/{session_id}/display",
    summary="保存 Agent 展示会话",
    response_model=_SchemaResponse[_SchemaAgentChatSessionSummary],
)
async def save_agent_chat_display(
    session_id: str,
    payload: _SchemaAgentChatDisplaySaveRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
) -> _SchemaResponse:
    """
    保存前端聚合后的 Agent 展示消息。

    :param session_id: Agent 会话 ID
    :param payload: 展示消息保存请求
    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :return: 保存后的会话摘要
    """
    existing_chat = await service.get_accessible(session_id, current_user)
    if existing_chat is None:
        unrestricted_chat = await service.get(session_id)
    else:
        unrestricted_chat = existing_chat
    if unrestricted_chat and existing_chat is None:
        return _SchemaResponse(success=False, message="会话不存在或无权访问")

    messages = [
        message.model_dump(exclude_none=True)
        for message in payload.messages
    ]
    await run_in_threadpool(
        _save_web_agent_display_snapshot,
        session_id=session_id,
        current_user=current_user,
        messages=messages,
        client_session_id=existing_chat.client_session_id if existing_chat else session_id,
    )
    chat = await service.get_accessible(session_id, current_user)
    if not chat:
        return _SchemaResponse(success=False, message="会话保存失败")
    return _SchemaResponse(success=True, data=service.to_summary(chat))


@router.delete(
    "/sessions/{session_id}",
    summary="删除 Agent 历史会话",
    response_model=_SchemaResponse[None],
)
async def delete_agent_chat_session(
    session_id: str,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
) -> _SchemaResponse:
    """
    删除一条 Agent 历史会话。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :return: 删除结果
    """
    chat = await _get_accessible_agent_chat(service, session_id, current_user)
    if not chat:
        return _SchemaResponse(success=False, message="会话不存在或无权访问")
    deleted = await service.delete(session_id, current_user)
    return _SchemaResponse(success=deleted, message="删除成功" if deleted else "删除失败")


@router.post(
    "/sessions/{session_id}/stop",
    summary="停止 Web 智能助手当前任务",
    response_model=_SchemaResponse[_SchemaAgentSessionStopData],
)
async def stop_web_agent_session_task(
    session_id: str,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
) -> _SchemaResponse:
    """
    停止当前 Web 智能助手会话正在执行的任务。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :return: 停止结果
    """
    server_session_id = _build_web_agent_session_id(current_user, session_id)
    chat = await _get_accessible_agent_chat(
        service,
        server_session_id,
        current_user,
    )
    if not chat and server_session_id != session_id:
        chat = await _get_accessible_agent_chat(service, session_id, current_user)
    if chat and not _can_access_agent_chat(chat, current_user):
        return _SchemaResponse(success=False, message="会话不存在或无权访问")

    manager = get_running_agent_manager()
    stopped = await manager.stop_current_task(server_session_id) if manager else False
    return _SchemaResponse(
        success=True,
        data={"stopped": stopped},
        message="已停止" if stopped else "当前没有正在执行的任务",
    )


@router.post(
    "/stream",
    summary="Web智能助手流式对话",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Agent SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def web_agent_stream(
    payload: _SchemaAgentWebChatRequest,
    request: Request,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> StreamingResponse:
    """
    Web 智能助手流式对话。

    :param payload: 对话请求
    :param request: 当前 HTTP 请求
    :param current_user: 当前登录用户
    :return: SSE 流式响应
    """
    prompt = payload.text.strip()
    locale = LocaleHelper.get_locale_from_request(request)
    display_prompt = (payload.display_text or payload.text).strip()
    session_id = _build_web_agent_session_id(current_user, payload.session_id)
    is_secret_confirmation_candidate = (
        prompt in {"确认", "取消"}
        and not payload.images
        and not payload.audio_refs
        and not payload.files
    )
    is_secret_confirmation_control = (
        is_secret_confirmation_candidate
        and (manager := get_running_agent_manager()) is not None
        and manager.matches_secret_confirmation(
            session_id,
            str(current_user.id),
            channel=NotificationChannel.WebAgent.value,
            source=WEB_AGENT_SOURCE,
        )
    )
    protected_transport_supported = (
        getattr(request, "headers", {}).get("X-MoviePilot-Agent-Interaction") == "1"
    )
    if is_secret_confirmation_control and not protected_transport_supported:
        return _build_web_agent_error_response(
            "当前客户端不支持安全交付敏感设置，未执行操作。",
            locale=locale,
        )
    is_traditional_message = (
        _is_web_agent_traditional_message(prompt)
        or _has_web_agent_traditional_interaction(str(current_user.id))
    )
    if is_traditional_message:
        denied_message = _ensure_web_agent_command_allowed(current_user)
        if denied_message:
            return _build_web_agent_error_response(
                denied_message,
                locale=locale,
            )
        unknown_command_message = _get_web_agent_unknown_command_message(prompt)
        if unknown_command_message:
            return _build_web_agent_error_response(
                unknown_command_message,
                locale=locale,
            )

        user_attachments = _build_web_agent_input_attachments(
            images=payload.images or [],
            files=[
                file.model_dump(exclude_none=True)
                for file in (payload.files or [])
            ],
            audio_refs=payload.audio_refs or [],
        )
        display_messages = []
        if payload.echo_user:
            display_messages.append(
                build_display_message(
                    role="user",
                    content=display_prompt or prompt,
                    attachments=user_attachments,
                )
            )

        async def traditional_event_generator() -> AsyncIterator[str]:
            """
            生成传统消息链路的 WebAgent SSE 事件。
            """
            yield _build_web_agent_sse(
                "start",
                {"session_id": session_id},
                locale=locale,
            )
            collection_task = asyncio.create_task(
                _collect_web_agent_traditional_events(
                    text=prompt,
                    current_user=current_user,
                    original_message_id=payload.original_message_id,
                    original_chat_id=payload.original_chat_id,
                )
            )
            try:
                while True:
                    try:
                        events = await asyncio.wait_for(
                            asyncio.shield(collection_task),
                            timeout=WEB_AGENT_STREAM_HEARTBEAT_SECONDS,
                        )
                        break
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            collection_task.cancel()
                            return
                        yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                if not collection_task.done():
                    collection_task.cancel()
                return

            assistant_message = _build_web_agent_display_message_from_events(events)
            display_messages.append(assistant_message)

            async def save_display_snapshot() -> None:
                """后台保存传统消息展示快照，不阻塞 SSE 终态。"""
                try:
                    await run_in_threadpool(
                        _save_web_agent_display_snapshot,
                        session_id=session_id,
                        current_user=current_user,
                        messages=display_messages,
                        client_session_id=payload.session_id or session_id,
                    )
                except Exception as err:
                    logger.error(f"保存WebAgent传统消息快照失败: {str(err)}")

            snapshot_task = asyncio.create_task(save_display_snapshot())
            _WEB_AGENT_BACKGROUND_TASKS.add(snapshot_task)
            snapshot_task.add_done_callback(_WEB_AGENT_BACKGROUND_TASKS.discard)
            await asyncio.sleep(0)
            for event in events:
                event_payload = copy.deepcopy(event)
                yield _build_web_agent_sse(
                    event_payload.pop("type"),
                    event_payload,
                    locale=locale,
                )
                if await request.is_disconnected():
                    return
            yield _build_web_agent_sse("done", {}, locale=locale)

        return build_sse_response(traditional_event_generator())

    if not get_api_runtime_config_snapshot().ai_agent_enable:
        return _build_web_agent_error_response(
            "智能助手未启用，请先在系统设置中开启。",
            locale=locale,
        )

    manager = get_running_agent_manager()
    if manager is None:
        return _build_web_agent_error_response(
            "智能助手服务尚未就绪，请稍后重试。",
            locale=locale,
        )

    transcript = await _transcribe_web_agent_audio_input(payload.audio_refs or [])
    prompt = _merge_web_agent_prompt_with_transcript(prompt, transcript)
    display_prompt = _merge_web_agent_prompt_with_transcript(display_prompt, transcript)
    has_audio_input = bool(transcript)
    if not prompt and payload.audio_refs and not payload.images and not payload.files:
        return _build_web_agent_error_response(
            "语音识别失败，请稍后重试。",
            locale=locale,
        )
    if not prompt and not payload.images and not payload.files and not payload.audio_refs:
        return _build_web_agent_error_response(
            "请输入要发送给智能助手的内容或选择附件。",
            locale=locale,
        )

    MessageChain().bind_user_session(str(current_user.id), session_id)
    event_publisher = _WebAgentEventPublisher()
    user_attachments = _build_web_agent_input_attachments(
        images=payload.images or [],
        files=[
            file.model_dump(exclude_none=True)
            for file in (payload.files or [])
        ],
        audio_refs=payload.audio_refs or [],
    )
    display_messages = []
    if payload.echo_user and not is_secret_confirmation_control:
        user_display_message = build_display_message(
            role="user",
            content=display_prompt or prompt,
            attachments=user_attachments,
        )
        if payload.choice_selection:
            user_display_message["choice_selection"] = payload.choice_selection
        display_messages.append(user_display_message)
    assistant_display_message = build_display_message(
        role="assistant",
        status="streaming",
    )
    display_messages.append(assistant_display_message)

    def output_callback(delta: str) -> None:
        """
        接收 Agent 文本增量并转换成 SSE 事件。
        """
        for item in _split_web_agent_output(delta):
            _apply_web_agent_display_event(item, assistant_display_message)
            event_publisher.publish(item)

    def message_callback(message: _SchemaMessage) -> None:
        """
        接收 Agent 工具主动发送的 Web 通知。
        """
        for item in _build_web_agent_message_events(message):
            _apply_web_agent_display_event(item, assistant_display_message)
            event_publisher.publish(item)

    def protected_output_callback(content: str) -> bool:
        """将敏感文本封装为不进入普通展示快照的命名 SSE 事件。"""
        return event_publisher.publish(
            {
                "type": "interaction-protected",
                "content": content,
            }
        )

    async def event_generator() -> AsyncIterator[str]:
        """
        生成前端 Agent SSE 事件。
        """
        audio_ref_set = set(payload.audio_refs or [])
        files = [
            file.model_dump(exclude_none=True)
            for file in (payload.files or [])
            if file.ref not in audio_ref_set
        ]
        for audio_ref in payload.audio_refs or []:
            files.append({"ref": audio_ref, "mime_type": "audio/*"})

        async def run_agent() -> None:
            """后台执行 Agent，并将结果写入事件队列。"""
            try:
                runtime_manager = get_running_agent_manager()
                if runtime_manager is None:
                    raise RuntimeError("智能助手服务尚未就绪，请稍后重试。")
                await runtime_manager.process_message(
                    session_id=session_id,
                    user_id=str(current_user.id),
                    message=prompt,
                    images=payload.images or [],
                    files=files or None,
                    has_audio_input=has_audio_input,
                    channel=NotificationChannel.WebAgent.value,
                    source=WEB_AGENT_SOURCE,
                    username=current_user.name,
                    reply_mode=ReplyMode.CAPTURE_ONLY,
                    allow_message_tools=True,
                    output_callback=output_callback,
                    protected_output_callback=(
                        protected_output_callback
                        if protected_transport_supported
                        else None
                    ),
                    message_callback=message_callback,
                    agent_factory=_get_web_agent_type(),
                    wait_for_completion=True,
                )
            except asyncio.CancelledError:
                # 显式停止会话沿用正常终止语义；服务关闭会由 manager 的稳定异常分支处理。
                pass
            except Exception as err:
                logger.error(f"Web智能助手执行失败: {str(err)}")
                error_event = {
                    "type": "error",
                    "message": f"智能助手执行失败: {str(err)}",
                }
                _apply_web_agent_display_event(error_event, assistant_display_message)
                event_publisher.publish(error_event)
            finally:
                done_event = {"type": "done"}
                _apply_web_agent_display_event(done_event, assistant_display_message)
                # 终态先进入 SSE 队列，避免展示快照落库延迟前端结束动画。
                event_publisher.publish(done_event)
                if not is_secret_confirmation_control:
                    await run_in_threadpool(
                        _save_web_agent_display_snapshot,
                        session_id=session_id,
                        current_user=current_user,
                        messages=display_messages,
                        client_session_id=payload.session_id or session_id,
                    )

        task = asyncio.create_task(run_agent())
        _WEB_AGENT_BACKGROUND_TASKS.add(task)
        task.add_done_callback(_WEB_AGENT_BACKGROUND_TASKS.discard)
        disconnected = False
        terminal_sent = False
        try:
            yield _build_web_agent_sse(
                "start",
                {"session_id": session_id},
                locale=locale,
            )
            while not global_vars.is_system_stopped:
                if await request.is_disconnected():
                    disconnected = True
                    break
                try:
                    event = await asyncio.wait_for(
                        event_publisher.get(),
                        timeout=WEB_AGENT_STREAM_HEARTBEAT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                event_type = str(event.get("type") or "")
                if event_type == "done":
                    terminal_sent = True
                yield _build_web_agent_sse(
                    event_type,
                    {key: value for key, value in event.items() if key != "type"},
                    locale=locale,
                )
                if event_type == "done":
                    break
        except asyncio.CancelledError:
            disconnected = True
            return
        finally:
            if not task.done() and not disconnected and not terminal_sent:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await event_publisher.aclose()
            # 客户端断线后保留 Agent 继续执行；发布器关闭后不再接受受保护结果。

    return build_sse_response(
        event_generator(),
        headers={
            **(
                {"X-MoviePilot-Agent-Control": "secret-confirmation"}
                if is_secret_confirmation_control
                else {}
            ),
        },
    )
