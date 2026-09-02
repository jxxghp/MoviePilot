import asyncio
import copy
import hashlib
import mimetypes
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from threading import Lock
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Tuple,
    Union,
    cast,
)

import aiofiles  # type: ignore[import-untyped]

from app.application import agent as agent_application
from app.application.agent import is_audio_input_available, transcribe_audio
from app.application.commands import dispatch_command, get_command, get_commands
from app.application.configuration import get_api_runtime_config_snapshot
from app.application.messaging.chat import (
    AgentChatPersistenceService,
    AgentChatPrincipal,
    AgentChatRecord,
    AgentChatService,
    get_configured_agent_chat_persistence,
    get_configured_agent_chat_service,
)
from app.application.messaging.router import has_pending_interaction
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.runtime.tasks import get_task_registry
from app.schemas.message import Message
from app.schemas.types import NotificationChannel, ReplyMode

__all__ = ["dispatch_command"]
# Agent 选择按钮回调前缀（新旧两种格式都必须继续兼容）
AGENT_CHOICE_PREFIX = "agent_interaction:choice:"
LEGACY_AGENT_CHOICE_PREFIX = "agent_choice:"


def build_agent_choice_callback(request_id: str, option_index: int) -> str:
    """构造 Agent 选择按钮回调数据。"""
    return f"{AGENT_CHOICE_PREFIX}{request_id}:{option_index}"


def parse_agent_choice_callback(
    callback_data: str,
) -> Optional[Tuple[str, int]]:
    """解析新旧两种 Agent 选择回调，格式无效时返回 None。"""
    if callback_data.startswith(AGENT_CHOICE_PREFIX):
        try:
            _, _, request_id, option_index = callback_data.split(":", 3)
        except ValueError:
            return None
    elif callback_data.startswith(LEGACY_AGENT_CHOICE_PREFIX):
        # 兼容旧格式，避免已发送的按钮失效
        try:
            _, request_id, option_index = callback_data.split(":", 2)
        except ValueError:
            return None
    else:
        return None
    if not request_id or not option_index.isdigit():
        return None
    return request_id, int(option_index)


def build_agent_choice_button_rows(
    request: "PendingAgentInteraction",
) -> Tuple[List[dict[str, Any]], List[List[dict[str, Any]]]]:
    """根据待选择请求构造 WebAgent 和消息渠道共用的按钮。"""
    buttons = [
        {
            "label": option.label,
            "callback_data": build_agent_choice_callback(request.request_id, index),
            "description": option.description or option.label,
        }
        for index, option in enumerate(request.options, start=1)
    ]
    button_rows = [[button] for button in buttons]
    return buttons, button_rows


@dataclass(frozen=True)
class AgentInteractionOption:
    """
    Agent 交互选项。
    """

    label: str
    value: str
    description: Optional[str] = None


@dataclass
class PendingAgentInteraction:
    """
    待处理的 Agent 客户端交互请求。
    """

    request_id: str
    session_id: str
    user_id: str
    channel: Optional[str]
    source: Optional[str]
    username: Optional[str]
    title: Optional[str]
    prompt: str
    options: List[AgentInteractionOption]
    created_at: datetime = field(default_factory=datetime.now)


class AgentInteractionManager:
    """
    管理 Agent 发起的客户端交互请求。
    """

    _ttl = timedelta(hours=24)

    def __init__(self):
        """初始化待处理的 Agent 交互请求表。"""
        self._pending_interactions: Dict[str, PendingAgentInteraction] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        """在持锁状态下移除过期 Agent 交互。"""
        expire_before = datetime.now() - self._ttl
        expired_ids = [
            request_id
            for request_id, request in self._pending_interactions.items()
            if request.created_at < expire_before
        ]
        for request_id in expired_ids:
            self._pending_interactions.pop(request_id, None)

    def create_request(
        self,
        session_id: str,
        user_id: str,
        channel: Optional[str],
        source: Optional[str],
        username: Optional[str],
        title: Optional[str],
        prompt: str,
        options: List[AgentInteractionOption],
    ) -> PendingAgentInteraction:
        """
        创建一条待用户确认的 Agent 交互请求。
        """
        with self._lock:
            self._cleanup_locked()
            request_id = uuid.uuid4().hex[:12]
            while request_id in self._pending_interactions:
                request_id = uuid.uuid4().hex[:12]
            request = PendingAgentInteraction(
                request_id=request_id,
                session_id=session_id,
                user_id=str(user_id),
                channel=channel,
                source=source,
                username=username,
                title=title,
                prompt=prompt,
                options=options,
            )
            self._pending_interactions[request_id] = request
            return request

    def resolve(
        self,
        request_id: str,
        option_index: int,
        user_id: Optional[str] = None,
    ) -> Optional[tuple[PendingAgentInteraction, AgentInteractionOption]]:
        """
        消费一条 Agent 交互请求，并返回选中的选项。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._pending_interactions.get(request_id)
            if not request:
                return None
            if user_id is not None and str(request.user_id) != str(user_id):
                return None
            if option_index < 1 or option_index > len(request.options):
                return None
            option = request.options[option_index - 1]
            self._pending_interactions.pop(request_id, None)
            return request, option

    def clear(self) -> None:
        """
        清空所有 Agent 交互请求。
        """
        with self._lock:
            self._pending_interactions.clear()


agent_interaction_manager = AgentInteractionManager()


_WEB_AGENT_EDIT_QUEUES: dict[str, list[Queue[dict[str, Any]]]] = {}
_WEB_AGENT_EDIT_LOCK = Lock()
_WEB_AGENT_MESSAGE_QUEUES: dict[str, list[Queue[Message]]] = {}
_WEB_AGENT_MESSAGE_LOCK = Lock()
_ChannelAdminResolver = Callable[[Optional[dict[str, Any]]], Iterable[Union[str, int]]]
_CHANNEL_ADMIN_RESOLVERS: dict[str, _ChannelAdminResolver] = {}
_WEB_AGENT_BACKGROUND_TASKS: set[asyncio.Task[object]] = set()


def create_web_agent_background_task(
    coroutine: Awaitable[object],
) -> asyncio.Task[object]:
    """登记 Web Agent 后台任务，使应用关闭时可以统一收口。"""
    task = cast(
        asyncio.Task[object],
        get_task_registry().create(
            coroutine,
            owner="api.agent.web_execution",
        ),
    )
    _WEB_AGENT_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_WEB_AGENT_BACKGROUND_TASKS.discard)
    return task


async def shutdown_web_agent_background_tasks() -> None:
    """取消并等待 Web Agent 后台任务，避免关闭数据库后仍提交快照。"""
    tasks = tuple(_WEB_AGENT_BACKGROUND_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        # asyncio.wait 不会因关闭阶段自身被取消而再次取消这些任务；仍在收尾的
        # Agent 任务会保留在注册表中，直到自己的数据库操作取得确定终态。
        await asyncio.wait(tasks)


async def wait_web_agent_background_tasks() -> None:
    """等待已登记的 Web Agent 任务完成最终收尾。"""
    tasks = tuple(_WEB_AGENT_BACKGROUND_TASKS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def register_channel_admin_resolver(
    channel: Union[NotificationChannel, str],
    resolver: _ChannelAdminResolver,
) -> None:
    """
    注册消息渠道的管理员主体 ID 解析器。

    :param channel: 消息渠道
    :param resolver: 由渠道配置解析全部管理员主体 ID 的函数
    """
    channel_value = channel.value if isinstance(channel, NotificationChannel) else str(channel)
    _CHANNEL_ADMIN_RESOLVERS[channel_value] = resolver


def resolve_config_principal_ids(
    config: Optional[dict[str, Any]],
    *config_keys: str,
) -> set[str]:
    """
    从渠道自行声明的配置键中解析主体 ID。

    :param config: 当前消息渠道配置
    :param config_keys: 由渠道模块维护的主体 ID 配置键
    :return: 去空白后的主体 ID 集合
    """
    principal_ids: set[str] = set()
    for config_key in config_keys:
        principal_ids.update(
            item.strip() for item in str((config or {}).get(config_key) or "").split(",") if item.strip()
        )
    return principal_ids


def matches_channel_admin(
    channel: Union[NotificationChannel, str],
    config: Optional[dict[str, Any]],
    *principal_ids: Optional[Union[str, int]],
) -> bool:
    """
    按渠道配置中的稳定主体 ID 判断管理员身份。

    :param channel: 消息渠道
    :param config: 当前消息渠道配置
    :param principal_ids: 消息渠道提供的稳定用户主体 ID
    :return: 任一用户主体 ID 命中渠道注册的管理员集合时返回 True
    """
    channel_value = channel.value if isinstance(channel, NotificationChannel) else str(channel)
    resolver = _CHANNEL_ADMIN_RESOLVERS.get(channel_value)
    if not resolver:
        return False
    authorized_ids = {
        str(principal_id).strip()
        for principal_id in resolver(config)
        if principal_id is not None and str(principal_id).strip()
    }
    if not authorized_ids:
        return False
    candidates = {
        str(principal_id).strip()
        for principal_id in principal_ids
        if principal_id is not None and str(principal_id).strip()
    }
    return bool(authorized_ids.intersection(candidates))


def normalize_web_agent_button_rows(buttons: Optional[list[list[dict[str, Any]]]]) -> list[list[dict[str, Any]]]:
    """
    将消息按钮转换为 WebAgent 前端可识别的按钮行。

    :param buttons: 传统消息模块返回的按钮二维数组
    :return: WebAgent 前端选项按钮二维数组
    """
    button_rows: list[list[dict[str, Any]]] = []
    for row in buttons or []:
        normalized_row = []
        for button in row or []:
            label = str(button.get("text") or button.get("label") or "").strip()
            callback_data = str(button.get("callback_data") or "").strip()
            if not label or not callback_data:
                continue
            normalized_button = {
                "label": label,
                "callback_data": callback_data,
            }
            if button.get("description"):
                normalized_button["description"] = str(button.get("description"))
            normalized_row.append(normalized_button)
        if normalized_row:
            button_rows.append(normalized_row)
    return button_rows


def _resolve_web_agent_choice_id(
    message_id: Union[str, int],
    button_rows: list[list[dict[str, Any]]],
) -> str:
    """
    从按钮回调中提取稳定的 WebAgent 选项 ID。

    :param message_id: 前端助手消息 ID
    :param button_rows: 已规范化的按钮行
    :return: 选项卡片 ID
    """
    for row in button_rows:
        for button in row:
            callback_data = str(button.get("callback_data") or "").strip()
            if not callback_data:
                continue
            parts = callback_data.split(":")
            if len(parts) >= 2 and parts[1]:
                return parts[1]
            return callback_data
    return str(message_id)


def build_web_agent_message_update_event(
    *,
    message_id: Union[str, int],
    title: Optional[str],
    text: str,
    buttons: Optional[list[list[dict[str, Any]]]],
) -> dict[str, Any]:
    """
    构造 WebAgent 原消息更新事件。

    :param message_id: 前端助手消息 ID
    :param title: 更新后的标题
    :param text: 更新后的正文
    :param buttons: 更新后的按钮
    :return: 前端可应用到原消息的 SSE 事件
    """
    button_rows = normalize_web_agent_button_rows(buttons)
    content_parts = [part for part in (title, text) if part]
    target_message = {
        "id": str(message_id),
        "content": "" if button_rows else "\n\n".join(content_parts),
        "choices": [],
        "attachments": [],
        "tools": [],
        "status": "done",
    }
    if button_rows:
        target_message["choices"].append(
            {
                "id": _resolve_web_agent_choice_id(message_id, button_rows),
                "title": title,
                "prompt": text or "",
                "buttons": [button for row in button_rows for button in row],
                "button_rows": button_rows,
                "status": "pending",
            }
        )
    return {
        "type": "message_update",
        "target_message": target_message,
    }


def extract_web_agent_message_from_event_data(data: dict[str, Any]) -> Optional[Message]:
    """
    从 NoticeMessage 事件数据中提取 WebAgent 通知。

    :param data: NoticeMessage 事件数据，兼容扁平字段和 message 包装格式
    :return: WebAgent 通知，不属于 WebAgent 或数据无效时返回 None
    """
    if not isinstance(data, dict):
        return None

    try:
        message = data.get("message")
        if isinstance(message, Message):
            message = message
        elif isinstance(message, dict):
            message_data = copy.deepcopy(message)
            message_data.pop("type", None)
            message = Message(**message_data)
        else:
            message_data = copy.deepcopy(data)
            message_data.pop("type", None)
            message_data.pop("current_time", None)
            message = Message(**message_data)
    except Exception as err:
        logger.debug(f"解析WebAgent通知事件失败: {err}")
        return None

    channel = message.channel
    channel_value = channel.value if isinstance(channel, NotificationChannel) else channel
    if channel_value != NotificationChannel.WebAgent.value:
        return None
    return message


def is_web_agent_message_for_user(message: Message, user_id: str) -> bool:
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


def _get_web_agent_message_user_id(message: Message) -> Optional[str]:
    """返回 WebAgent 通知的目标用户 ID，无目标时返回 None。"""
    try:
        channel = message.channel
        channel_value = channel.value if isinstance(channel, NotificationChannel) else channel
        if channel_value != NotificationChannel.WebAgent.value:
            return None
        user_id = message.userid
        return str(user_id) if user_id is not None else None
    except Exception:
        return None


def dispatch_web_agent_message_event(event: object) -> None:
    """将 WebAgent NoticeMessage 分发给正在等待的请求队列。"""
    event_data = getattr(event, "event_data", None)
    data = event_data if isinstance(event_data, dict) else {}
    message = extract_web_agent_message_from_event_data(data)
    if not message:
        return
    with _WEB_AGENT_MESSAGE_LOCK:
        user_id = _get_web_agent_message_user_id(message)
        if user_id is None:
            queues = [
                message_queue for user_queues in _WEB_AGENT_MESSAGE_QUEUES.values() for message_queue in user_queues
            ]
        else:
            queues = list(_WEB_AGENT_MESSAGE_QUEUES.get(user_id) or [])
    for message_queue in queues:
        message_queue.put(message)


def attach_web_agent_message_queue(user_id: str, message_queue: Queue[Message]) -> None:
    """为当前 WebAgent 请求挂载通知收集队列。"""
    with _WEB_AGENT_MESSAGE_LOCK:
        _WEB_AGENT_MESSAGE_QUEUES.setdefault(str(user_id), []).append(message_queue)


def detach_web_agent_message_queue(user_id: str, message_queue: Queue[Message]) -> None:
    """移除当前 WebAgent 请求的通知收集队列。"""
    with _WEB_AGENT_MESSAGE_LOCK:
        queues = _WEB_AGENT_MESSAGE_QUEUES.get(str(user_id))
        if not queues:
            return
        _WEB_AGENT_MESSAGE_QUEUES[str(user_id)] = [item for item in queues if item is not message_queue]
        if not _WEB_AGENT_MESSAGE_QUEUES[str(user_id)]:
            _WEB_AGENT_MESSAGE_QUEUES.pop(str(user_id), None)


def attach_web_agent_edit_queue(user_id: str, edit_queue: Queue[dict[str, Any]]) -> None:
    """
    为当前 WebAgent 请求挂载原消息编辑事件队列。

    :param user_id: 当前用户 ID
    :param edit_queue: 用于接收编辑事件的队列
    """
    with _WEB_AGENT_EDIT_LOCK:
        _WEB_AGENT_EDIT_QUEUES.setdefault(str(user_id), []).append(edit_queue)


def detach_web_agent_edit_queue(user_id: str, edit_queue: Queue[dict[str, Any]]) -> None:
    """
    移除当前 WebAgent 请求的原消息编辑事件队列。

    :param user_id: 当前用户 ID
    :param edit_queue: 需要移除的队列
    """
    with _WEB_AGENT_EDIT_LOCK:
        queues = _WEB_AGENT_EDIT_QUEUES.get(str(user_id))
        if not queues:
            return
        _WEB_AGENT_EDIT_QUEUES[str(user_id)] = [item for item in queues if item is not edit_queue]
        if not _WEB_AGENT_EDIT_QUEUES[str(user_id)]:
            _WEB_AGENT_EDIT_QUEUES.pop(str(user_id), None)


def dispatch_web_agent_edit_event(
    *,
    user_id: str,
    event: dict[str, Any],
) -> bool:
    """
    将 WebAgent 原消息编辑事件分发给正在等待的请求队列。

    :param user_id: 当前用户 ID
    :param event: 前端可应用的 SSE 事件
    :return: 是否存在接收本次编辑事件的请求队列
    """
    with _WEB_AGENT_EDIT_LOCK:
        queues = list(_WEB_AGENT_EDIT_QUEUES.get(str(user_id)) or [])
    for edit_queue in queues:
        edit_queue.put(event)
    return bool(queues)


def edit_web_agent_message(
    *,
    user_id: str,
    message_id: Union[str, int],
    title: Optional[str],
    text: str,
    buttons: Optional[list[list[dict[str, Any]]]] = None,
) -> bool:
    """
    原地更新 WebAgent 前端消息卡片。

    :param user_id: 当前用户 ID
    :param message_id: 前端助手消息 ID
    :param title: 更新后的标题
    :param text: 更新后的正文
    :param buttons: 更新后的按钮
    :return: 是否已投递编辑事件
    """
    if not user_id:
        return False
    event = build_web_agent_message_update_event(
        message_id=message_id,
        title=title,
        text=text,
        buttons=buttons,
    )
    return dispatch_web_agent_edit_event(user_id=user_id, event=event)


WEB_AGENT_SESSION_PREFIX = "web-agent:"
WEB_AGENT_SOURCE = "web-agent"
WEB_AGENT_FILE_TTL_SECONDS = 6 * 60 * 60
WEB_AGENT_FILE_MAX_ITEMS = 256
WEB_AGENT_UPLOAD_MAX_BYTES = 32 * 1024 * 1024
WEB_AGENT_UPLOAD_CHUNK_SIZE = 1024 * 1024
WEB_AGENT_AUDIO_CONVERSION_TIMEOUT_SECONDS = 60.0
WEB_AGENT_BROWSER_AUDIO_SUFFIXES = {".aac", ".m4a", ".mp3", ".mp4", ".wav", ".wave"}
WEB_AGENT_TRADITIONAL_IDLE_TIMEOUT_SECONDS = 2.0
WEB_AGENT_TRADITIONAL_MAX_WAIT_SECONDS = 60.0
WEB_AGENT_STREAM_HEARTBEAT_SECONDS = 15.0
WEB_AGENT_STREAM_COALESCE_SECONDS = 0.03
WEB_AGENT_STREAM_COALESCE_MAX_CHARS = 256
WEB_AGENT_STREAM_QUEUE_MAX_SIZE = 64
_WEB_AGENT_FILE_REGISTRY: dict[str, dict[str, Any]] = {}
_web_agent_message_handler: Optional[Callable[..., object]] = None
_web_agent_session_binder: Optional[Callable[[str, str], None]] = None


def configure_web_agent_message_runtime(
    *,
    message_handler: Callable[..., object],
    session_binder: Callable[[str, str], None],
) -> None:
    """由启动组合根装配传统消息执行与会话绑定端口。"""
    global _web_agent_message_handler, _web_agent_session_binder
    _web_agent_message_handler = message_handler
    _web_agent_session_binder = session_binder


def reset_web_agent_message_runtime() -> None:
    """清除 WebAgent 消息运行端口，避免 lifespan 重启复用旧对象。"""
    global _web_agent_message_handler, _web_agent_session_binder
    _web_agent_message_handler = None
    _web_agent_session_binder = None


def _handle_web_agent_message(**kwargs: Any) -> object:
    """调用已装配的传统消息入口。"""
    if _web_agent_message_handler is None:
        raise RuntimeError("WebAgent 传统消息入口尚未配置")
    return _web_agent_message_handler(**kwargs)


def bind_web_agent_user_session(user_id: str, session_id: str) -> None:
    """把 Web 用户绑定到服务端 Agent 会话。"""
    if _web_agent_session_binder is None:
        raise RuntimeError("WebAgent 会话绑定端口尚未配置")
    _web_agent_session_binder(user_id, session_id)


class WebAgentUpload(Protocol):
    """WebAgent 上传源所需的最小异步读取合同。"""

    async def read(self, size: int = -1) -> bytes:
        """读取下一段上传内容。"""
        ...

    async def close(self) -> None:
        """关闭上传源。"""
        ...


class WebAgentUploadTooLargeError(ValueError):
    """上传附件超过 WebAgent 单文件容量限制。"""


@dataclass(frozen=True, slots=True)
class WebAgentStreamCommand:
    """WebAgent 流式对话用例所需的传输无关输入。"""

    text: str = ""
    display_text: Optional[str] = None
    session_id: Optional[str] = None
    images: list[str] = field(default_factory=list)
    audio_refs: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    choice_selection: Optional[dict[str, Any]] = None
    original_message_id: Optional[Union[str, int]] = None
    original_chat_id: Optional[Union[str, int]] = None
    echo_user: bool = True


@dataclass(frozen=True, slots=True)
class WebAgentStreamResult:
    """WebAgent 流式用例返回的事件迭代器与传输控制标记。"""

    events: AsyncIterator[dict[str, Any]]
    control: Optional[str] = None


def build_web_agent_display_message(
    role: str,
    content: str = "",
    attachments: Optional[list[dict[str, Any]]] = None,
    status: str = "done",
) -> dict[str, Any]:
    """构造 WebAgent 前后端共享的展示消息。"""
    normalized_content = content or ""
    return {
        "id": f"{role}-{uuid.uuid4().hex}",
        "role": role,
        "content": normalized_content,
        "createdAt": int(datetime.now().timestamp() * 1000),
        "status": status,
        "tools": [],
        "segments": ([{"type": "text", "content": normalized_content}] if normalized_content else []),
        "attachments": attachments or [],
        "choices": [],
    }


class WebAgentEventPublisher:
    """合并 WebAgent 文本增量，并通过有界队列向 SSE 消费者提供事件。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=WEB_AGENT_STREAM_QUEUE_MAX_SIZE)
        self._pending_events: deque[dict[str, Any]] = deque()
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

    def publish(self, event: dict[str, Any]) -> bool:
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

    async def get(self) -> dict[str, Any]:
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

    def _append_event(self, event: dict[str, Any]) -> None:
        """追加待发布事件，相邻文本在出口阻塞时继续合并。"""
        if event.get("type") == "delta" and self._pending_events and self._pending_events[-1].get("type") == "delta":
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


def build_web_agent_session_id(user: AgentChatPrincipal, session_id: Optional[str]) -> str:
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


async def build_web_agent_session_id_async(
    user: AgentChatPrincipal,
    session_id: Optional[str],
    service: Optional[AgentChatService] = None,
) -> str:
    """异步解析 Web Agent 会话 ID，并复用异步会话查询端口。"""
    seed = str(session_id or "").strip() or uuid.uuid4().hex
    if seed.startswith(WEB_AGENT_SESSION_PREFIX):
        return seed
    try:
        if service is None:
            service = get_configured_agent_chat_service()
        existing_chat = await service.get(seed)
        if existing_chat and AgentChatService.can_access(existing_chat, user):
            return seed
    except Exception as e:
        logger.debug(f"读取WebAgent历史会话失败: {e}")
    user_part = user.name or str(user.id)
    digest = hashlib.sha256(f"{user_part}:{seed}".encode("utf-8")).hexdigest()
    return f"{WEB_AGENT_SESSION_PREFIX}{digest[:32]}"


def can_access_agent_chat(chat: Any, user: AgentChatPrincipal) -> bool:
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


async def get_accessible_agent_chat(
    service: AgentChatService,
    session_id: str,
    user: AgentChatPrincipal,
) -> Optional[AgentChatRecord]:
    """
    读取当前用户可访问的 Agent 会话。
    """
    return await service.get_accessible(session_id, user)


def append_web_agent_text_segment(assistant_message: dict[str, Any], content: str) -> None:
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


def build_legacy_web_agent_segments(content: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    为未携带有序片段的旧展示消息生成兼容布局。

    :param content: 聚合后的助手文本
    :param tools: 工具提示列表
    :return: 按旧版工具在前、文本在后的顺序生成的片段
    """
    segments = [{"type": "tool", "toolIndex": index} for index in range(len(tools))]
    if content:
        segments.append({"type": "text", "content": content})
    return segments


def apply_web_agent_display_event(event: dict[str, Any], assistant_message: dict[str, Any]) -> None:
    """
    将 WebAgent SSE 事件同步应用到服务端展示消息快照。
    """
    event_type = event.get("type")
    if event_type == "delta":
        append_web_agent_text_segment(assistant_message, event.get("content") or "")
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
        assistant_message.setdefault("segments", []).append({"type": "tool", "toolIndex": tool_index})
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
            else build_legacy_web_agent_segments(assistant_message["content"], assistant_message["tools"])
        )
        assistant_message["status"] = target_message.get("status") or "done"
    elif event_type == "error":
        assistant_message["status"] = "error"
        if not assistant_message["content"]:
            append_web_agent_text_segment(
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


async def save_web_agent_display_snapshot(
    *,
    session_id: str,
    current_user: AgentChatPrincipal,
    messages: list[dict[str, Any]],
    client_session_id: Optional[str] = None,
    service: Optional[AgentChatService] = None,
    persistence: Optional[AgentChatPersistenceService] = None,
) -> None:
    """
    保存 WebAgent 当前展示消息快照。
    """
    if service is None:
        # 直接调用该内部 helper 时没有 FastAPI 依赖注入上下文。
        service = get_configured_agent_chat_service()
    existing_chat = await service.get(session_id)
    if persistence is None:
        # 直接调用该内部 helper 时没有 FastAPI 依赖注入上下文。
        persistence = get_configured_agent_chat_persistence()
    await persistence.async_save_display_messages(
        session_id=session_id,
        user_id=(existing_chat.user_id if existing_chat else str(current_user.id)),
        username=(existing_chat.username if existing_chat else current_user.name),
        channel=(existing_chat.channel if existing_chat and existing_chat.channel else NotificationChannel.WebAgent),
        source=(existing_chat.source if existing_chat and existing_chat.source else WEB_AGENT_SOURCE),
        original_chat_id=existing_chat.original_chat_id if existing_chat else None,
        client_session_id=(
            existing_chat.client_session_id if existing_chat and existing_chat.client_session_id else client_session_id
        ),
        messages=messages,
    )


def sanitize_web_agent_upload_name(filename: Optional[str], mime_type: Optional[str] = None) -> str:
    """
    规范化 Web Agent 上传文件名，避免路径穿越和空文件名。

    :param filename: 浏览器上传的原始文件名
    :param mime_type: 浏览器上报的 MIME 类型
    :return: 可安全落盘的文件名
    """
    name = Path(filename or "attachment").name.strip()
    safe_name = "".join(char for char in name if char.isalnum() or char in (" ", ".", "_", "-")).strip(" .")
    if not safe_name:
        safe_name = "attachment"
    if "." not in safe_name:
        suffix = mimetypes.guess_extension(mime_type or "") or ""
        safe_name = f"{safe_name}{suffix}"
    return safe_name


async def get_web_agent_upload_dir(
    user: AgentChatPrincipal,
    session_id: Optional[str],
    service: Optional[AgentChatService] = None,
) -> Path:
    """
    计算当前 Web Agent 会话的临时附件目录。

    :param user: 当前登录用户
    :param session_id: 前端会话标识
    :return: 已创建的临时附件目录
    """
    server_session_id = await build_web_agent_session_id_async(
        user,
        session_id,
        service,
    )
    safe_session_id = server_session_id.replace(":", "_")
    upload_dir = Path(get_api_runtime_config_snapshot().temp_path) / "agent_uploads" / safe_session_id
    await run_in_threadpool(upload_dir.mkdir, parents=True, exist_ok=True)
    return upload_dir


async def save_web_agent_upload(upload_file: WebAgentUpload, target_path: Path) -> int:
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
                    raise WebAgentUploadTooLargeError("附件超过 32MB，无法发送给智能助手")
                await output.write(chunk)
    except Exception:
        await run_in_threadpool(target_path.unlink, missing_ok=True)
        raise
    finally:
        await upload_file.close()
    return size


def cleanup_web_agent_file_registry() -> None:
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


def guess_web_agent_attachment_kind(mime_type: Optional[str], fallback: str = "file") -> str:
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


def build_web_agent_url_attachment(
    url: str,
    kind: str,
    name: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> dict[str, Any]:
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


def build_web_agent_input_attachments(
    images: list[str],
    files: list[dict[str, Any]],
    audio_refs: list[str],
) -> list[dict[str, Any]]:
    """
    构造 WebAgent 用户输入附件展示记录。
    """
    attachments: list[dict[str, Any]] = []
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
                "kind": guess_web_agent_attachment_kind(mime_type),
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


def register_web_agent_file(
    file_path: Optional[str],
    file_name: Optional[str] = None,
    kind: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Optional[dict[str, Any]]:
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

    cleanup_web_agent_file_registry()
    file_id = uuid.uuid4().hex
    display_name = file_name or resolved_path.name
    resolved_mime_type = mime_type or mimetypes.guess_type(display_name or str(resolved_path))[0]
    file_url = f"message/agent/file/{file_id}"
    _WEB_AGENT_FILE_REGISTRY[file_id] = {
        "path": resolved_path,
        "name": display_name,
        "mime_type": resolved_mime_type or "application/octet-stream",
        "created_at": time.time(),
    }
    return {
        "kind": kind or guess_web_agent_attachment_kind(resolved_mime_type),
        "url": file_url,
        "download_url": file_url,
        "name": display_name,
        "mime_type": resolved_mime_type,
        "size": resolved_path.stat().st_size,
    }


def get_web_agent_audio_mime_type(audio_path: Path) -> Optional[str]:
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


def resolve_web_agent_audio_source_path(voice_path: str) -> Path:
    """解析语音源文件；文件暂时不可用时保留原始路径以维持回退语义。"""
    try:
        return Path(voice_path).expanduser().resolve(strict=True)
    except OSError:
        return Path(voice_path)


def remove_web_agent_audio_output(path: Path) -> None:
    """清理未完成的转码产物。"""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as err:
        logger.debug("WebAgent 清理未完成语音转码产物失败: path=%s, error=%s", path, err)


async def terminate_web_agent_audio_process(
    process: Optional[asyncio.subprocess.Process],
) -> None:
    """终止并回收转码进程，避免取消或超时留下孤儿 ffmpeg。"""
    if process is None or process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        await process.communicate()
    except (OSError, ProcessLookupError):
        pass


async def prepare_web_agent_audio_attachment_path_async(voice_path: str) -> Path:
    """异步准备 WebAgent 语音附件，转码等待可取消且有超时。"""
    source_path = Path(
        await run_in_threadpool(
            resolve_web_agent_audio_source_path,
            voice_path,
        )
    )
    if source_path.suffix.lower() in WEB_AGENT_BROWSER_AUDIO_SUFFIXES:
        return source_path

    ffmpeg_path = await run_in_threadpool(shutil.which, "ffmpeg")
    if not ffmpeg_path:
        logger.warning("WebAgent 语音转 WAV 跳过：ffmpeg 不可用，path=%s", source_path)
        return source_path

    voice_dir = Path(get_api_runtime_config_snapshot().temp_path) / "voice"
    try:
        await run_in_threadpool(voice_dir.mkdir, parents=True, exist_ok=True)
    except OSError as err:
        logger.warning("WebAgent 语音转 WAV 目录不可用，将回退原文件: path=%s, error=%s", voice_dir, err)
        return source_path

    output_path = voice_dir / f"{source_path.stem}_web_{uuid.uuid4().hex[:8]}.wav"
    process: Optional[asyncio.subprocess.Process] = None

    async def cleanup_conversion_output() -> None:
        """清理失败或取消的转码进程及临时产物。"""
        await terminate_web_agent_audio_process(process)
        await run_in_threadpool(remove_web_agent_audio_output, output_path)

    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path,
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
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=WEB_AGENT_AUDIO_CONVERSION_TIMEOUT_SECONDS,
        )
        output_exists = await run_in_threadpool(output_path.exists)
        if process.returncode != 0 or not output_exists:
            await cleanup_conversion_output()
            logger.warning(
                "WebAgent 语音转 WAV 失败，将回退原文件: returncode=%s, stderr=%s",
                process.returncode,
                (stderr or b"").decode("utf-8", errors="replace").strip()[:500],
            )
            return source_path
        return Path(output_path)
    except asyncio.TimeoutError:
        await cleanup_conversion_output()
        logger.warning(
            "WebAgent 语音转 WAV 超时，将回退原文件: timeout=%ss, path=%s",
            WEB_AGENT_AUDIO_CONVERSION_TIMEOUT_SECONDS,
            source_path,
        )
        return source_path
    except asyncio.CancelledError:
        await cleanup_conversion_output()
        logger.warning("WebAgent 语音转 WAV 已取消，path=%s", source_path)
        raise
    except OSError as err:
        await cleanup_conversion_output()
        logger.warning("WebAgent 语音转 WAV 启动失败，将回退原文件: path=%s, error=%s", source_path, err)
        return source_path


def get_web_agent_registered_file(ref: str) -> Optional[dict[str, Any]]:
    """
    根据前端附件引用读取 WebAgent 临时文件登记信息。

    :param ref: message/agent/file/{file_id} 形式的短期引用
    :return: 文件登记信息，引用无效或过期时返回 None
    """
    normalized_ref = (ref or "").strip()
    prefix = "message/agent/file/"
    if not normalized_ref.startswith(prefix):
        return None

    cleanup_web_agent_file_registry()
    file_id = normalized_ref[len(prefix) :].split("/", 1)[0]
    file_info = _WEB_AGENT_FILE_REGISTRY.get(file_id)
    if not file_info:
        return None
    file_path = Path(file_info["path"])
    if not file_path.exists() or not file_path.is_file():
        _WEB_AGENT_FILE_REGISTRY.pop(file_id, None)
        return None
    return file_info


def resolve_web_agent_audio_refs(
    audio_refs: list[str],
) -> list[tuple[str, Path, str]]:
    """在调用协程中解析音频引用，返回不依赖登记表的文件快照。"""
    audio_files = []
    for audio_ref in audio_refs:
        file_info = get_web_agent_registered_file(audio_ref)
        if not file_info:
            logger.warning("WebAgent 语音引用不存在或已过期: ref=%s", audio_ref)
            continue

        file_path = Path(file_info["path"])
        audio_files.append((audio_ref, file_path, file_info.get("name") or file_path.name))
    return audio_files


def transcribe_web_agent_audio_files(
    audio_files: list[tuple[str, Path, str]],
) -> Optional[str]:
    """
    转写 WebAgent 上传的本地录音附件。

    文件信息已在调用协程中从短期登记表解析，阻塞文件读取和 provider 调用可在
    worker 中执行，避免跨线程访问登记表。
    """
    if not audio_files:
        return None
    if not is_audio_input_available():
        logger.warning("WebAgent 音频输入能力未配置或未启用，跳过语音识别")
        return None

    transcripts = []
    for audio_ref, file_path, file_name in audio_files:
        try:
            content = file_path.read_bytes()
        except OSError as err:
            logger.warning("WebAgent 语音文件读取失败: ref=%s, error=%s", audio_ref, err)
            continue

        transcript = transcribe_audio(
            content=content,
            filename=file_name,
        )
        if transcript:
            transcripts.append(transcript)

    return "\n".join(transcripts).strip() if transcripts else None


async def transcribe_web_agent_audio_input(audio_refs: list[str]) -> Optional[str]:
    """解析并在线程池中转写 WebAgent 音频引用。"""
    audio_files = resolve_web_agent_audio_refs(audio_refs)
    if not audio_files:
        return None
    return await asyncio.to_thread(transcribe_web_agent_audio_files, audio_files)


def merge_web_agent_prompt_with_transcript(prompt: str, transcript: Optional[str]) -> str:
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


def build_web_agent_choice_event(message: Message) -> Optional[dict[str, Any]]:
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


def resolve_web_agent_choice_payload(callback_data: str, user_id: str) -> Optional[dict[str, Any]]:
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


def build_web_agent_message_events(
    message: Message,
    *,
    prepared_audio_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """
    将 Agent 工具通知转换为 Web SSE 事件。

    :param message: 工具产生的通知消息
    :return: 前端可直接应用到当前助手消息的事件列表
    """
    events = []
    choice_event = build_web_agent_choice_event(message)
    if choice_event:
        events.append(choice_event)

    text_parts = [str(item).strip() for item in (message.title, message.text) if str(item or "").strip()]
    if text_parts and not choice_event:
        events.append({"type": "delta", "content": "\n\n".join(text_parts)})

    if message.image:
        image_ref = message.image
        image_path = Path(image_ref).expanduser()
        attachment = None
        if not image_ref.startswith(("http://", "https://", "data:", "blob:")):
            attachment = register_web_agent_file(image_ref, file_name=Path(image_ref).name, kind="image")
        if not attachment:
            attachment = build_web_agent_url_attachment(
                image_ref,
                kind="image",
                name=message.title or image_path.name or "image",
            )
        events.append({"type": "attachment", "attachment": attachment})

    if message.voice_path:
        audio_path = prepared_audio_path or Path(message.voice_path)
        attachment = register_web_agent_file(
            str(audio_path),
            file_name=audio_path.name,
            kind="audio",
            mime_type=get_web_agent_audio_mime_type(audio_path),
        )
        if attachment:
            events.append({"type": "attachment", "attachment": attachment})

    if message.file_path:
        attachment = register_web_agent_file(
            message.file_path,
            file_name=message.file_name or Path(message.file_path).name,
        )
        if attachment:
            events.append({"type": "attachment", "attachment": attachment})

    return events


async def build_web_agent_message_events_async(
    message: Message,
) -> list[dict[str, Any]]:
    """异步构造 WebAgent 通知，确保语音转码不占用事件循环。"""
    prepared_audio_path = None
    if message.voice_path:
        prepared_audio_path = await prepare_web_agent_audio_attachment_path_async(message.voice_path)
    return build_web_agent_message_events(
        message,
        prepared_audio_path=prepared_audio_path,
    )


def build_web_agent_display_message_from_events(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    将传统消息事件聚合为前端展示消息快照。

    :param events: 已转换的 WebAgent SSE 事件列表
    :return: 可持久化的助手展示消息
    """
    message = build_web_agent_display_message(
        role="assistant",
        status="streaming",
    )
    for event in events:
        apply_web_agent_display_event(copy.deepcopy(event), message)
    apply_web_agent_display_event({"type": "done"}, message)
    return message


def split_web_agent_output(text: str) -> list[dict[str, Any]]:
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

        after_marker = remaining[marker_index + len(marker) :]
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


def is_web_agent_traditional_message(text: str) -> bool:
    """
    判断用户输入是否应走传统消息命令/交互链路。

    :param text: 前端输入文本
    :return: 需要交给 MessageChain 时返回 True
    """
    normalized = str(text or "").strip()
    return normalized.startswith("/") or normalized.startswith("CALLBACK:")


def has_web_agent_traditional_interaction(user_id: str) -> bool:
    """
    判断当前用户是否存在待继续的传统交互会话。

    :param user_id: 当前登录用户 ID
    :return: 存在传统交互上下文时返回 True
    """
    return bool(has_pending_interaction(user_id))


def build_web_agent_command_items() -> list[dict[str, Any]]:
    """
    读取当前可用斜杠命令并转换为前端建议列表。

    :return: 按分类和命令名排序的命令列表
    """
    commands = get_commands() or {}
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

def extract_web_agent_slash_command(text: str) -> Optional[str]:
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


def get_web_agent_unknown_command_message(text: str) -> Optional[str]:
    """
    判断 WebAgent 斜杠命令是否不存在。

    :param text: 前端输入文本
    :return: 命令不存在时返回错误提示，命令存在或非命令时返回 None
    """
    command = extract_web_agent_slash_command(text)
    if not command:
        return None
    if get_command(command):
        return None
    return f"命令不存在：{command}"


def ensure_web_agent_command_allowed(current_user: AgentChatPrincipal) -> Optional[str]:
    """
    校验当前 Web 用户是否可以执行传统斜杠命令。

    :param current_user: 当前登录用户
    :return: 无权限时返回错误提示，允许执行时返回 None
    """
    if getattr(current_user, "is_superuser", False):
        return None
    return "只有管理员才有权限执行此命令"


async def collect_web_agent_traditional_events(
    *,
    text: str,
    current_user: AgentChatPrincipal,
    original_message_id: Optional[Union[str, int]] = None,
    original_chat_id: Optional[Union[str, int]] = None,
) -> list[dict[str, Any]]:
    """
    执行传统消息链路并收集本次 WebAgent 用户产生的通知事件。

    :param text: 需要交给传统消息链路处理的文本
    :param current_user: 当前登录用户
    :param original_message_id: WebAgent 原助手消息 ID
    :param original_chat_id: WebAgent 原聊天 ID
    :return: 可直接发送给前端的 SSE 事件列表
    """
    message_queue: Queue[Message] = Queue()
    edit_queue: Queue[dict[str, Any]] = Queue()
    user_id = str(current_user.id)

    attach_web_agent_message_queue(user_id, message_queue)
    attach_web_agent_edit_queue(user_id, edit_queue)
    try:
        await run_in_threadpool(
            _handle_web_agent_message,
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

            if not is_web_agent_message_for_user(message, user_id):
                continue
            events.extend(await build_web_agent_message_events_async(message))
            idle_deadline = time.monotonic() + WEB_AGENT_TRADITIONAL_IDLE_TIMEOUT_SECONDS
        return events
    finally:
        detach_web_agent_message_queue(user_id, message_queue)
        detach_web_agent_edit_queue(user_id, edit_queue)


def build_web_agent_traditional_callback_payload(
    callback_data: str,
    original_message_id: Optional[Union[str, int]] = None,
    original_chat_id: Optional[Union[str, int]] = None,
) -> dict[str, Any]:
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


async def _single_web_agent_event(event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """把前置校验结果包装成单事件异步流。"""
    yield event


def _build_traditional_web_agent_stream(
    *,
    command: WebAgentStreamCommand,
    current_user: AgentChatPrincipal,
    session_id: str,
    prompt: str,
    display_prompt: str,
    service: AgentChatService,
    persistence: AgentChatPersistenceService,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[dict[str, Any]]:
    """构造传统消息链路的 WebAgent 事件流。"""
    user_attachments = build_web_agent_input_attachments(
        images=command.images,
        files=command.files,
        audio_refs=command.audio_refs,
    )
    display_messages = []
    if command.echo_user:
        display_messages.append(
            build_web_agent_display_message(
                role="user",
                content=display_prompt or prompt,
                attachments=user_attachments,
            )
        )

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        """等待传统消息结果并投影为传输无关事件。"""
        yield {"type": "start", "session_id": session_id}
        collection_task = asyncio.create_task(
            collect_web_agent_traditional_events(
                text=prompt,
                current_user=current_user,
                original_message_id=command.original_message_id,
                original_chat_id=command.original_chat_id,
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
                    if await is_disconnected():
                        collection_task.cancel()
                        return
                    yield {"type": "heartbeat"}
        except asyncio.CancelledError:
            return
        finally:
            if not collection_task.done():
                collection_task.cancel()
                await asyncio.gather(collection_task, return_exceptions=True)

        display_messages.append(build_web_agent_display_message_from_events(events))

        async def save_display_snapshot() -> None:
            """后台保存传统消息展示快照，不阻塞事件流终态。"""
            try:
                await save_web_agent_display_snapshot(
                    session_id=session_id,
                    current_user=current_user,
                    messages=display_messages,
                    client_session_id=command.session_id or session_id,
                    service=service,
                    persistence=persistence,
                )
            except Exception as err:
                logger.error(f"保存WebAgent传统消息快照失败: {str(err)}")

        create_web_agent_background_task(save_display_snapshot())
        await asyncio.sleep(0)
        for event in events:
            yield copy.deepcopy(event)
            if await is_disconnected():
                return
        yield {"type": "done"}

    return event_generator()


def _build_agent_web_agent_stream(
    *,
    command: WebAgentStreamCommand,
    current_user: AgentChatPrincipal,
    session_id: str,
    prompt: str,
    display_prompt: str,
    has_audio_input: bool,
    is_secret_confirmation_control: bool,
    protected_transport_supported: bool,
    service: AgentChatService,
    persistence: AgentChatPersistenceService,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[dict[str, Any]]:
    """构造标准 Agent 执行链路的 WebAgent 事件流。"""
    bind_web_agent_user_session(str(current_user.id), session_id)
    event_publisher = WebAgentEventPublisher()
    user_attachments = build_web_agent_input_attachments(
        images=command.images,
        files=command.files,
        audio_refs=command.audio_refs,
    )
    display_messages = []
    if command.echo_user and not is_secret_confirmation_control:
        user_display_message = build_web_agent_display_message(
            role="user",
            content=display_prompt or prompt,
            attachments=user_attachments,
        )
        if command.choice_selection:
            user_display_message["choice_selection"] = command.choice_selection
        display_messages.append(user_display_message)
    assistant_display_message = build_web_agent_display_message(
        role="assistant",
        status="streaming",
    )
    display_messages.append(assistant_display_message)

    def output_callback(delta: str) -> None:
        """接收 Agent 文本增量并投影为展示事件。"""
        for item in split_web_agent_output(delta):
            apply_web_agent_display_event(item, assistant_display_message)
            event_publisher.publish(item)

    async def message_callback(message: Message) -> None:
        """接收 Agent 工具主动发送的 Web 通知。"""
        for item in await build_web_agent_message_events_async(message):
            apply_web_agent_display_event(item, assistant_display_message)
            event_publisher.publish(item)

    def protected_output_callback(content: str) -> bool:
        """发布不进入普通展示快照的敏感交互结果。"""
        return event_publisher.publish({"type": "interaction-protected", "content": content})

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        """执行 Agent 并按断线与终态语义消费事件。"""
        audio_ref_set = set(command.audio_refs)
        files = [file for file in command.files if str(file.get("ref") or "") not in audio_ref_set]
        files.extend({"ref": audio_ref, "mime_type": "audio/*"} for audio_ref in command.audio_refs)

        async def run_agent() -> None:
            """后台执行 Agent，并在完成后持久化展示快照。"""
            try:
                runtime_manager = agent_application.get_running_agent_manager()
                if runtime_manager is None:
                    raise RuntimeError("智能助手服务尚未就绪，请稍后重试。")
                await runtime_manager.process_message(
                    session_id=session_id,
                    user_id=str(current_user.id),
                    message=prompt,
                    images=command.images,
                    files=files or None,
                    has_audio_input=has_audio_input,
                    channel=NotificationChannel.WebAgent.value,
                    source=WEB_AGENT_SOURCE,
                    username=current_user.name,
                    reply_mode=ReplyMode.CAPTURE_ONLY,
                    allow_message_tools=True,
                    output_callback=output_callback,
                    protected_output_callback=(protected_output_callback if protected_transport_supported else None),
                    message_callback=message_callback,
                    agent_factory=agent_application.get_web_agent_type(),
                    wait_for_completion=True,
                )
            except asyncio.CancelledError:
                # 显式停止会话沿用正常终止语义；服务关闭由 manager 的稳定异常分支处理。
                pass
            except Exception as err:
                logger.error(f"Web智能助手执行失败: {str(err)}")
                error_event = {
                    "type": "error",
                    "message": f"智能助手执行失败: {str(err)}",
                }
                apply_web_agent_display_event(error_event, assistant_display_message)
                event_publisher.publish(error_event)
            finally:
                done_event = {"type": "done"}
                apply_web_agent_display_event(done_event, assistant_display_message)
                # 终态先进入事件队列，避免展示快照落库延迟前端结束动画。
                event_publisher.publish(done_event)
                if not is_secret_confirmation_control:
                    try:
                        await save_web_agent_display_snapshot(
                            session_id=session_id,
                            current_user=current_user,
                            messages=display_messages,
                            client_session_id=command.session_id or session_id,
                            service=service,
                            persistence=persistence,
                        )
                    except Exception as err:
                        logger.error(f"保存WebAgent展示历史失败：{err}")

        task = create_web_agent_background_task(run_agent())
        disconnected = False
        terminal_sent = False
        try:
            yield {"type": "start", "session_id": session_id}
            while not runtime_stop_state.is_system_stopped:
                if await is_disconnected():
                    disconnected = True
                    break
                try:
                    event = await asyncio.wait_for(
                        event_publisher.get(),
                        timeout=WEB_AGENT_STREAM_HEARTBEAT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
                if event.get("type") == "done":
                    terminal_sent = True
                yield event
                if event.get("type") == "done":
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
            # 客户端断线后保留 Agent 继续执行；发布器关闭后拒绝受保护结果。

    return event_generator()


async def build_web_agent_stream(
    command: WebAgentStreamCommand,
    *,
    current_user: AgentChatPrincipal,
    is_disconnected: Callable[[], Awaitable[bool]],
    protected_transport_supported: bool,
    service: Optional[AgentChatService] = None,
    persistence: Optional[AgentChatPersistenceService] = None,
) -> WebAgentStreamResult:
    """编排 WebAgent 会话、传统消息、音频输入、事件投影与展示持久化。"""
    prompt = command.text.strip()
    display_prompt = (command.display_text or command.text).strip()
    service = service or get_configured_agent_chat_service()
    persistence = persistence or get_configured_agent_chat_persistence()
    session_id = await build_web_agent_session_id_async(
        current_user,
        command.session_id,
        service,
    )
    is_secret_confirmation_candidate = (
        prompt in {"确认", "取消"} and not command.images and not command.audio_refs and not command.files
    )
    manager = agent_application.get_running_agent_manager()
    is_secret_confirmation_control = (
        is_secret_confirmation_candidate
        and manager is not None
        and manager.matches_secret_confirmation(
            session_id,
            str(current_user.id),
            channel=NotificationChannel.WebAgent.value,
            source=WEB_AGENT_SOURCE,
        )
    )
    if is_secret_confirmation_control and not protected_transport_supported:
        return WebAgentStreamResult(
            events=_single_web_agent_event(
                {
                    "type": "error",
                    "message": "当前客户端不支持安全交付敏感设置，未执行操作。",
                }
            )
        )

    is_traditional_message = is_web_agent_traditional_message(prompt) or has_web_agent_traditional_interaction(
        str(current_user.id)
    )
    if is_traditional_message:
        denied_message = ensure_web_agent_command_allowed(current_user)
        if denied_message:
            return WebAgentStreamResult(events=_single_web_agent_event({"type": "error", "message": denied_message}))
        unknown_command_message = get_web_agent_unknown_command_message(prompt)
        if unknown_command_message:
            return WebAgentStreamResult(
                events=_single_web_agent_event({"type": "error", "message": unknown_command_message})
            )
        return WebAgentStreamResult(
            events=_build_traditional_web_agent_stream(
                command=command,
                current_user=current_user,
                session_id=session_id,
                prompt=prompt,
                display_prompt=display_prompt,
                service=service,
                persistence=persistence,
                is_disconnected=is_disconnected,
            )
        )

    if not get_api_runtime_config_snapshot().ai_agent_enable:
        return WebAgentStreamResult(
            events=_single_web_agent_event({"type": "error", "message": "智能助手未启用，请先在系统设置中开启。"})
        )
    if manager is None:
        return WebAgentStreamResult(
            events=_single_web_agent_event(
                {
                    "type": "error",
                    "message": "智能助手服务尚未就绪，请稍后重试。",
                }
            )
        )

    transcript = await transcribe_web_agent_audio_input(command.audio_refs)
    prompt = merge_web_agent_prompt_with_transcript(prompt, transcript)
    display_prompt = merge_web_agent_prompt_with_transcript(display_prompt, transcript)
    if not prompt and command.audio_refs and not command.images and not command.files:
        return WebAgentStreamResult(
            events=_single_web_agent_event({"type": "error", "message": "语音识别失败，请稍后重试。"})
        )
    if not prompt and not command.images and not command.files and not command.audio_refs:
        return WebAgentStreamResult(
            events=_single_web_agent_event(
                {
                    "type": "error",
                    "message": "请输入要发送给智能助手的内容或选择附件。",
                }
            )
        )

    return WebAgentStreamResult(
        events=_build_agent_web_agent_stream(
            command=command,
            current_user=current_user,
            session_id=session_id,
            prompt=prompt,
            display_prompt=display_prompt,
            has_audio_input=bool(transcript),
            is_secret_confirmation_control=is_secret_confirmation_control,
            protected_transport_supported=protected_transport_supported,
            service=service,
            persistence=persistence,
            is_disconnected=is_disconnected,
        ),
        control=("secret-confirmation" if is_secret_confirmation_control else None),
    )
