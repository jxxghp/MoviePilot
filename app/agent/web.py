"""WebAgent 运行时类型适配，不包含 HTTP 或 SSE 编码。"""

from threading import Lock
from typing import Any, Awaitable, Callable, Optional

from app.agent.loader import get_moviepilot_agent_type
from app.application.security.user import get_configured_user_id_lookup
from app.runtime.log import logger
from app.schemas.message import Message


class _WebAgentStreamingHandlerMixin:
    """
    Web 前端专用流式处理器，将工具提示和文本统一回调给 SSE。
    """

    _lock: Lock
    _pending_tool_stats: dict[str, int]

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
        # 该方法由运行时组合进 MRO 的 StreamingHandler 提供。
        super().record_tool_call(  # type: ignore[misc]
            tool_name=tool_name,
            tool_message=tool_message,
            tool_kwargs=tool_kwargs,
        )
        self.flush_pending_tool_summary()

    def emit(self, token: str) -> str:
        """追加 token 并同步通知 SSE 生产者。"""
        # 该方法由运行时组合进 MRO 的 StreamingHandler 提供。
        emitted = super().emit(token)  # type: ignore[misc]
        if emitted:
            self._on_emit(emitted)
        return str(emitted)

    def flush_pending_tool_summary(self) -> str:
        """输出延迟聚合的工具摘要。"""
        # 该方法由运行时组合进 MRO 的 StreamingHandler 提供。
        emitted = super().flush_pending_tool_summary()  # type: ignore[misc]
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

    is_background: bool
    output_callback: Optional[Callable[[str], None]]
    stream_handler: Any
    user_id: Optional[str]
    _streamed_output: str

    def __init__(
        self,
        *args: Any,
        message_callback: Optional[Callable[[Message], Awaitable[None] | None]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._message_callback = message_callback
        self.stream_handler = _get_web_agent_streaming_handler_type()(self._emit_output)

    def _should_stream(self) -> bool:
        """Web 对话实时输出，复用会话执行后台任务时改用非流式广播。"""
        if self.is_background:
            return False
        return True

    def set_message_callback(
        self,
        message_callback: Optional[Callable[[Message], Awaitable[None] | None]],
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
        if output_callback and isinstance(self.stream_handler, _WebAgentStreamingHandlerMixin):
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
        # 该方法由运行时组合进 MRO 的 MoviePilotAgent 提供。
        context = await super()._build_tool_context(  # type: ignore[misc]
            should_dispatch_reply
        )
        context["message_callback"] = self._message_callback
        return dict(context)

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
