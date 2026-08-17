import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, Union

from app.schemas.message import Message
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.types import NotificationChannel


@dataclass
class PendingSlashInteraction:
    """
    通用 slash 命令交互上下文。
    """

    request_id: str
    user_id: str
    channel: Optional[NotificationChannel]
    source: Optional[str]
    username: Optional[str]
    command: str
    page: int = 0
    awaiting_input: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


class SlashInteractionManager:
    """
    管理单个 slash 命令的交互会话。
    """

    _ttl = timedelta(hours=24)

    def __init__(self):
        """初始化按请求和用户索引的 slash 会话表。"""
        self._by_id: Dict[str, PendingSlashInteraction] = {}
        self._by_user: Dict[str, str] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        """在持锁状态下移除过期 slash 会话。"""
        expire_before = datetime.now() - self._ttl
        expired = [
            request_id
            for request_id, request in self._by_id.items()
            if request.created_at < expire_before
        ]
        for request_id in expired:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def create_or_replace(
            self,
            user_id: Union[str, int],
            command: str,
            channel: Optional[NotificationChannel],
            source: Optional[str],
            username: Optional[str],
    ) -> PendingSlashInteraction:
        """为用户创建 slash 会话并替换其上一条会话。"""
        with self._lock:
            self._cleanup_locked()
            user_key = str(user_id)
            old_request_id = self._by_user.get(user_key)
            if old_request_id:
                self._by_id.pop(old_request_id, None)
            request = PendingSlashInteraction(
                request_id=uuid.uuid4().hex[:12],
                user_id=user_key,
                command=command,
                channel=channel,
                source=source,
                username=username,
            )
            self._by_id[request.request_id] = request
            self._by_user[user_key] = request.request_id
            return request

    def get_by_user(
            self, user_id: Union[str, int]
    ) -> Optional[PendingSlashInteraction]:
        """按用户返回仍有效的 slash 会话。"""
        with self._lock:
            self._cleanup_locked()
            request_id = self._by_user.get(str(user_id))
            if not request_id:
                return None
            return self._by_id.get(request_id)

    def get_by_id(
            self, request_id: str, user_id: Union[str, int]
    ) -> Optional[PendingSlashInteraction]:
        """按请求 ID 和用户联合校验 slash 会话。"""
        with self._lock:
            self._cleanup_locked()
            request = self._by_id.get(request_id)
            if not request or str(request.user_id) != str(user_id):
                return None
            return request

    def remove(self, request_id: str) -> None:
        """删除指定 slash 会话及其用户索引。"""
        with self._lock:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def clear(self) -> None:
        """清空全部 slash 会话。"""
        with self._lock:
            self._by_id.clear()
            self._by_user.clear()


@dataclass(frozen=True, slots=True)
class InteractionContext:
    """描述一次与渠道无关的用户交互上下文。"""

    channel: NotificationChannel
    source: Optional[str]
    user_id: Union[str, int]
    username: Optional[str]
    original_message_id: Optional[Union[str, int]] = None
    original_chat_id: Optional[str] = None
    is_channel_admin: Optional[bool] = None


@dataclass(frozen=True, slots=True)
class InteractionDispatch:
    """描述交互路由是否命中以及是否延迟结束处理状态。"""

    handled: bool
    defer_processing_finish: bool = False


class MessageGateway(Protocol):
    """声明交互控制器使用的消息发送和编辑能力。"""

    def post_message(self, message: Message): ...

    def edit_message(self, **kwargs) -> bool: ...


def supports_interaction_buttons(channel: Optional[NotificationChannel]) -> bool:
    """
    渠道同时支持按钮和回调时，优先使用按钮交互。
    """
    return bool(
        channel
        and ChannelCapabilityManager.supports_buttons(channel)
        and ChannelCapabilityManager.supports_callbacks(channel)
    )


def supports_markdown(channel: Optional[NotificationChannel]) -> bool:
    """
    仅在支持 Markdown 的渠道上输出 Markdown 内容。
    """
    return bool(channel and ChannelCapabilityManager.supports_markdown(channel))


def page_items(
        items: Sequence[Any],
        page: int,
        page_size: int,
) -> Tuple[List[Any], int, int]:
    """
    对列表做分页并规范化页码。
    """
    total = len(items)
    if total == 0:
        return [], 0, 1
    total_pages = max(1, math.ceil(total / max(1, page_size)))
    page = min(max(0, page), total_pages - 1)
    start = page * page_size
    end = start + page_size
    return list(items[start:end]), page, total_pages


def build_navigation_buttons(
        prefix: str,
        request: Any,
        page: int,
        total_pages: int,
) -> List[List[dict]]:
    """
    构造标准上一页/下一页按钮。
    """
    buttons = []
    nav_row = []
    if page > 0:
        nav_row.append(
            {
                "text": "⬅️ 上一页",
                "callback_data": f"{prefix}:{request.request_id}:page-prev",
            }
        )
    if page < total_pages - 1:
        nav_row.append(
            {
                "text": "下一页 ➡️",
                "callback_data": f"{prefix}:{request.request_id}:page-next",
            }
        )
    if nav_row:
        buttons.append(nav_row)
    return buttons


def update_or_post_message(
        chain,
        channel: NotificationChannel,
        source: Optional[str],
        userid: Union[str, int],
        username: Optional[str],
        title: str,
        text: str,
        buttons: Optional[List[List[dict]]] = None,
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
) -> None:
    """
    优先编辑原消息，失败时回退为发送新消息。
    """
    if (
            original_message_id
            and original_chat_id
            and ChannelCapabilityManager.supports_editing(channel)
    ):
        edit_kwargs = {}
        if channel == NotificationChannel.WebAgent:
            edit_kwargs["metadata"] = {"userid": userid}
        edited = chain.edit_message(
            channel=channel,
            source=source,
            message_id=original_message_id,
            chat_id=original_chat_id,
            title=title,
            text=text,
            buttons=buttons,
            **edit_kwargs,
        )
        if edited:
            return

    chain.post_message(
        Message(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title=title,
            text=text,
            buttons=buttons,
            # 编辑失败回退发新消息时保留原消息上下文，
            # 保证飞书等渠道能回复到原会话（如群聊），而不是发给私聊。
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            save_history=False,
        )
    )


def escape_markdown_table_cell(value: object) -> str:
    """
    最小化转义 Markdown 表格中的特殊字符。
    """
    text = str(value or "").replace("\n", "<br>")
    return text.replace("|", "\\|")


def format_markdown_table(
        headers: Sequence[str],
        rows: Sequence[Sequence[object]],
) -> str:
    """
    生成 Markdown 表格文本。
    """
    header_line = (
            "| "
            + " | ".join(escape_markdown_table_cell(item) for item in headers)
            + " |"
    )
    separator_line = "| " + " | ".join("---" for _ in headers) + " |"
    data_lines = [
        "| "
        + " | ".join(escape_markdown_table_cell(item) for item in row)
        + " |"
        for row in rows
    ]
    return "\n".join([header_line, separator_line, *data_lines])
