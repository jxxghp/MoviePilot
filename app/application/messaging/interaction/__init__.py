"""
渠道交互的通用状态存储、消息端口与按钮展示工具。
"""

import math
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import wraps
from inspect import signature
from threading import Lock
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    ParamSpec,
    Protocol,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

from app.schemas.message import Message
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.types import NotificationChannel


class _UserInteractionEntry(Protocol):
    """
    声明按用户维护的交互状态所需的最小字段。

    :ivar request_id: 当前交互请求标识，用于校验按钮是否仍属于活动会话
    :ivar user_id: 所属用户标识，用于隔离不同用户的交互状态
    :ivar created_at: 会话创建的本地时间，用于计算过期时间
    """

    request_id: str
    user_id: str
    created_at: datetime


InteractionEntryT = TypeVar("InteractionEntryT", bound=_UserInteractionEntry)


@dataclass(frozen=True, slots=True)
class InteractionKey:
    """
    独立状态存储内的一条会话地址，每类交互使用自己的存储。

    :param channel: 平台，未指定平台的旧式本地操作独占一个空间
    :param instance: 宿主确认的实例 ID，未装配的兼容入口使用来源名称
    :param actor: 发起人平台 ID
    :param conversation: 会话地址；缺失只匹配同样缺失地址的入口
    :param thread: 会话内话题或线程；缺失只匹配无话题的入口
    """

    channel: Optional[NotificationChannel]
    instance: str
    actor: str
    conversation: str
    thread: str = ""


_interaction_scope: ContextVar[Optional[tuple[Optional[str], InteractionKey]]] = ContextVar("interaction_scope", default=None)
_InteractionParams = ParamSpec("_InteractionParams")
_InteractionResult = TypeVar("_InteractionResult")


def scoped_interaction(function: Callable[_InteractionParams, _InteractionResult]) -> Callable[_InteractionParams, _InteractionResult]:
    """
    为宿主消息和命令入口设置交互地址，退出时恢复外层上下文。
    """
    parameters = signature(function)

    @wraps(function)
    def wrapped(*args: _InteractionParams.args, **kwargs: _InteractionParams.kwargs) -> _InteractionResult:
        """
        按可信渠道配置确定稳定实例 ID，并保存入站会话和线程地址。
        """
        values = parameters.bind(*args, **kwargs).arguments
        channel, source, actor = values.get("channel"), values.get("source"), values.get("userid")
        conversation, thread = values.get("original_chat_id"), values.get("thread_id")
        key = _interaction_address(actor, channel, source, conversation, thread)
        token = _interaction_scope.set((source, key))
        try:
            return function(*args, **kwargs)
        finally:
            _interaction_scope.reset(token)

    return wrapped


def interaction_key(
        user_id: Optional[Union[str, int]], channel: Optional[NotificationChannel],
        source: Optional[str], chat_id: Optional[str] = None, *, thread_id: Optional[str] = None,
) -> InteractionKey:
    """
    在有界的宿主路由作用域中继承已确认实例和线程，旧处理器无需改变位置参数。

    仅继承同平台、同来源、同发送者的范围；显式会话和线程也必须一致。

    :param thread_id: 后台操作显式保留的原线程 ID
    """
    actor = str(user_id) if user_id is not None else ""
    scoped = _interaction_scope.get()
    if scoped is not None:
        scoped_source, key = scoped
        if (scoped_source == source and key.channel == channel and key.actor == actor
                and (chat_id is None or key.conversation == str(chat_id))
                and (thread_id is None or key.thread == thread_id)):
            return key
    return _interaction_address(user_id, channel, source, chat_id, thread_id)


def _interaction_address(
        actor: Optional[Union[str, int]], channel: Optional[NotificationChannel], source: Optional[str],
        conversation: Optional[str], thread: Optional[str] = None,
) -> InteractionKey:
    """
    由当前配置确定实例 ID。未装配的本地和兼容入口按平台与来源隔离。
    """
    from app.application.security.authorization import get_channel_authorization

    instance = f"source:{source or ''}"
    authorization = get_channel_authorization()
    if authorization and channel is not None and channel not in (NotificationChannel.Web, NotificationChannel.WebAgent) and source:
        try:
            instance = f"id:{authorization.notification(channel.value, source).id}"
        except PermissionError:
            # 兼容范围与已授权实例的键不同。消息入口仍会拒绝已停用的实例。
            pass
    return InteractionKey(channel, instance, str(actor) if actor is not None else "",
                          str(conversation or ""), str(thread or ""))


class _ExpiringUserInteractionStore(Generic[InteractionEntryT]):
    """
    在当前交互类别内按完整会话地址存储状态并校验回调归属。
    """

    _ttl = timedelta(hours=24)

    def __init__(self) -> None:
        """
        初始化按请求 ID、用户 ID 查询的线程安全状态表。
        """
        self._by_id: Dict[str, InteractionEntryT] = {}
        self._by_key: Dict[InteractionKey, str] = {}
        self._key_by_id: Dict[str, InteractionKey] = {}
        self._used_callbacks: dict[str, set[tuple[str, Optional[int], str]]] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        """
        在持锁状态下移除超过统一保留时间的交互状态。
        """
        expire_before = datetime.now() - self._ttl
        expired = [
            request_id
            for request_id, request in self._by_id.items()
            if request.created_at < expire_before
        ]
        for request_id in expired:
            self._remove_locked(request_id)

    def _replace_locked(self, request: InteractionEntryT, key: InteractionKey) -> None:
        """
        在持锁状态下替换用户旧状态并写入新的双向索引。
        """
        old_request_id = self._by_key.get(key)
        if old_request_id:
            self._remove_locked(old_request_id)
        self._by_id[request.request_id] = request
        self._by_key[key] = request.request_id
        self._key_by_id[request.request_id] = key

    def get_by_user(
            self, user_id: Optional[Union[str, int]], channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None, chat_id: Optional[str] = None,
    ) -> Optional[InteractionEntryT]:
        """
        按完整会话地址返回状态，裸用户参数只读取未指定渠道的本地空间。
        """
        key = interaction_key(user_id, channel, source, chat_id)
        with self._lock:
            self._cleanup_locked()
            request_id = self._by_key.get(key)
            if not request_id:
                return None
            return self._by_id.get(request_id)

    def get_by_id(
            self, request_id: str, user_id: Union[str, int], channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None, chat_id: Optional[str] = None, *, thread_id: Optional[str] = None,
    ) -> Optional[InteractionEntryT]:
        """
        按请求 ID 和完整会话地址校验请求仍属于当前交互。

        :param thread_id: 后台操作保留的原线程 ID
        """
        key = interaction_key(user_id, channel, source, chat_id, thread_id=thread_id)
        with self._lock:
            self._cleanup_locked()
            if self._key_by_id.get(request_id) != key:
                return None
            return self._by_id.get(request_id)

    def claim_callback(
            self, request: InteractionEntryT, action: str, index: Optional[int] = None, version: str = "",
    ) -> bool:
        """
        在已按完整地址取回请求之后，原子消费属于该请求的一个写按钮。

        :param request: 当前授权请求，关闭或已被替换时不能消费
        :param action: 已解析并验证的动作
        :param index: 操作的列表索引，操作无索引时为 None
        :param version: 渲染该按钮的快照标识，旧格式使用空字符串
        :return: 仅首次消费且请求当前有效时为 True
        """
        with self._lock:
            self._cleanup_locked()
            if self._by_id.get(request.request_id) is not request:
                return False
            identity = (action, index, version)
            used = self._used_callbacks.setdefault(request.request_id, set())
            if identity in used:
                return False
            used.add(identity)
            return True

    def _remove_locked(self, request_id: str) -> None:
        """
        持锁移除请求和对应的两个方向索引。
        """
        self._by_id.pop(request_id, None)
        self._used_callbacks.pop(request_id, None)
        key = self._key_by_id.pop(request_id, None)
        if key is not None:
            self._by_key.pop(key, None)

    def remove(self, request_id: str) -> None:
        """
        删除指定交互状态及其用户索引。
        """
        with self._lock:
            self._remove_locked(request_id)

    def clear(self) -> None:
        """
        清空全部交互状态，供生命周期结束和测试场景使用。
        """
        with self._lock:
            self._by_id.clear()
            self._by_key.clear()
            self._key_by_id.clear()
            self._used_callbacks.clear()


@dataclass
class PendingSlashInteraction:
    """
    通用 slash 命令交互上下文。

    :param request_id: 当前交互请求标识，用于校验按钮是否仍属于活动会话
    :param user_id: 所属用户标识，用于隔离不同用户的交互状态
    :param channel: 回复使用的通知渠道，None 表示未指定
    :param source: 渠道实例标识，None 表示未指定实例
    :param username: 用户显示名称，未知时为 None
    :param command: 当前处理的斜杠命令名称
    :param page: 当前列表页索引，从 0 开始
    :param awaiting_input: 当前等待的文本输入用途，None 表示未等待补充输入
    :param created_at: 会话创建的本地时间，用于计算过期时间
    :param write_version: 当前写按钮所在视图的版本，未出卡时为空
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
    write_version: str = ""


class SlashInteractionManager(
        _ExpiringUserInteractionStore[PendingSlashInteraction]
):
    """
    管理单个 slash 命令的交互会话。
    """

    def create_or_replace(
            self,
            user_id: Optional[Union[str, int]],
            command: str,
            channel: Optional[NotificationChannel],
            source: Optional[str],
            username: Optional[str],
            original_chat_id: Optional[str] = None,
    ) -> PendingSlashInteraction:
        """
        为用户创建 slash 会话并替换其上一条会话。
        """
        with self._lock:
            self._cleanup_locked()
            user_key = str(user_id)
            request = PendingSlashInteraction(
                request_id=uuid.uuid4().hex[:12],
                user_id=user_key,
                command=command,
                channel=channel,
                source=source,
                username=username,
            )
            self._replace_locked(request, interaction_key(user_id, channel, source, original_chat_id))
            return request


@dataclass(frozen=True, slots=True)
class InteractionContext:
    """
    描述一次与渠道无关的用户交互上下文。

    :param channel: 接收本次交互的通知渠道
    :param source: 渠道实例标识，None 表示未指定实例
    :param user_id: 所属用户标识，用于隔离不同用户的交互状态
    :param username: 用户显示名称，未知时为 None
    :param original_message_id: 渠道原始消息标识，未知时为 None
    :param original_chat_id: 渠道原始聊天标识，未知时为 None
    :param is_channel_admin: 渠道管理员身份判断；None 表示尚未判定
    """

    channel: NotificationChannel
    source: Optional[str]
    user_id: Union[str, int]
    username: Optional[str]
    original_message_id: Optional[Union[str, int]] = None
    original_chat_id: Optional[str] = None
    is_channel_admin: Optional[bool] = None


@dataclass(frozen=True, slots=True)
class InteractionDispatch:
    """
    描述交互路由是否命中以及是否延迟结束处理状态。

    :param handled: 是否已由本次路由消费交互
    :param defer_processing_finish: 是否由后续异步流程结束渠道处理状态，默认立即结束
    """

    handled: bool
    defer_processing_finish: bool = False


class MessageGateway(Protocol):
    """
    声明交互控制器使用的消息发送和编辑能力。
    """

    def post_message(self, message: Message) -> None:
        """
        发送一条交互消息。

        :param message: 包含渠道、接收用户和展示内容的消息
        """
        ...

    def edit_message(
            self,
            channel: NotificationChannel,
            source: Optional[str],
            message_id: Union[str, int],
            chat_id: Union[str, int],
            text: str,
            title: Optional[str] = None,
            buttons: Optional[List[List[Dict[str, Any]]]] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        编辑一条已发送的交互消息。

        :param channel: 原消息所属通知渠道
        :param source: 原消息所属渠道实例标识
        :param message_id: 渠道分配的原消息标识
        :param chat_id: 原消息所在的聊天标识
        :param text: 替换后的消息正文
        :param title: 替换后的标题，None 表示不提供独立标题
        :param buttons: 按行组织的新按钮，None 表示不提供按钮
        :param metadata: 渠道特定的附加编辑参数，None 表示无附加参数
        :return: 渠道确认编辑成功时为 True；失败或不支持编辑时为 False
        """
        ...


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
        request: _UserInteractionEntry,
        page: int,
        total_pages: int,
) -> List[List[dict[str, str]]]:
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
        chain: MessageGateway,
        channel: Optional[NotificationChannel],
        source: Optional[str],
        userid: Optional[Union[str, int]],
        username: Optional[str],
        title: str,
        text: str,
        buttons: Optional[List[List[dict[str, str]]]] = None,
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
) -> None:
    """
    优先编辑原消息，失败时回退为发送新消息。

    :param thread_id: 发送新消息时使用的原话题
    """
    if (
            original_message_id
            and original_chat_id
            and channel is not None
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
            thread_id=thread_id,
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
