"""
Agent 按钮选择交互契约与进程内待处理请求管理。
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Optional

# Agent 选择按钮回调前缀，新旧两种格式都必须继续兼容。
AGENT_CHOICE_PREFIX = "agent_interaction:choice:"
LEGACY_AGENT_CHOICE_PREFIX = "agent_choice:"


def is_agent_learning_command(text: str | None) -> bool:
    """
    仅识别专用学习命令前缀；把它们交给 Agent 用户控制入口而非传统管理员命令。
    """
    return bool(re.match(r'^/(?:memory(?:\s|$)|skills\s+(?:adopt|pin|unpin|restore)(?:\s|$))', str(text or '').strip()))


def build_agent_choice_callback(request_id: str, option_index: int) -> str:
    """
    构造 Agent 选择按钮回调数据。
    """
    return f"{AGENT_CHOICE_PREFIX}{request_id}:{option_index}"


def parse_agent_choice_callback(
    callback_data: str,
) -> Optional[tuple[str, int]]:
    """
    解析新旧两种 Agent 选择回调，格式无效时返回 None。
    """
    if callback_data.startswith(AGENT_CHOICE_PREFIX):
        try:
            _, _, request_id, option_index = callback_data.split(":", 3)
        except ValueError:
            return None
    elif callback_data.startswith(LEGACY_AGENT_CHOICE_PREFIX):
        # 兼容旧格式，避免已发送的按钮失效。
        try:
            _, request_id, option_index = callback_data.split(":", 2)
        except ValueError:
            return None
    else:
        return None
    if not request_id or not option_index.isdigit():
        return None
    return request_id, int(option_index)


@dataclass(frozen=True)
class AgentInteractionOption:
    """
    Agent 交互选项。

    :param label: 按钮向用户展示的选项名称
    :param value: 选中后传回 Agent 的实际值
    :param description: 选项的补充说明，None 表示没有说明
    """

    label: str
    value: str
    description: Optional[str] = None


@dataclass
class PendingAgentInteraction:
    """
    待处理的 Agent 客户端交互请求。

    :param request_id: 当前交互请求标识，用于校验按钮是否仍属于活动会话
    :param session_id: 等待用户选择的 Agent 会话标识
    :param user_id: 所属用户标识，用于隔离不同用户的交互状态
    :param channel: 回复使用的通知渠道，None 表示未指定
    :param source: 渠道实例标识，None 表示未指定实例
    :param username: 用户显示名称，未知时为 None
    :param title: 交互展示标题，None 表示不单独显示标题
    :param prompt: 向用户说明选择目的的提示正文
    :param options: 按展示顺序排列的可选项
    :param created_at: 会话创建的本地时间，用于计算过期时间
    :ivar selected_index: 已确认的选项序号；未选择时为 None
    :ivar selection_persisted: Web 选择结果是否已完成持久化
    :ivar execution_claimed: Web 选择是否已交给一次 Agent 执行
    """

    request_id: str
    session_id: str
    user_id: str
    channel: Optional[str]
    source: Optional[str]
    username: Optional[str]
    title: Optional[str]
    prompt: str
    options: list[AgentInteractionOption]
    created_at: datetime = field(default_factory=datetime.now)
    selected_index: Optional[int] = field(default=None, init=False)
    selection_persisted: bool = field(default=False, init=False)
    execution_claimed: bool = field(default=False, init=False)


def build_agent_choice_button_rows(
    request: PendingAgentInteraction,
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """
    根据待选择请求构造 WebAgent 和消息渠道共用的按钮。
    """
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


class AgentInteractionManager:
    """
    管理 Agent 发起的客户端交互请求。
    """

    _ttl = timedelta(hours=24)

    def __init__(self) -> None:
        """
        初始化待处理的 Agent 交互请求表。
        """
        self._pending_interactions: dict[str, PendingAgentInteraction] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        """
        在持锁状态下移除过期 Agent 交互。
        """
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
        options: list[AgentInteractionOption],
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

    def get_request(self, request_id: str, user_id: str) -> Optional[PendingAgentInteraction]:
        """
        读取当前用户的交互归属，供异步会话授权使用，不消费请求。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._pending_interactions.get(request_id)
            return request if request and request.user_id == str(user_id) else None

    def resolve_web_choice(
        self, request_id: str, option_index: int, user_id: str,
    ) -> Optional[tuple[PendingAgentInteraction, AgentInteractionOption, bool]]:
        """
        保留 Web 选择收据；重复按钮始终返回首次选项。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._pending_interactions.get(request_id)
            if not request or request.user_id != str(user_id) or not 1 <= option_index <= len(request.options):
                return None
            repeated = request.execution_claimed
            if request.selected_index is None:
                request.selected_index = option_index
            return request, request.options[request.selected_index - 1], repeated

    def finish_web_choice(self, request_id: str, user_id: str, *, persisted: bool) -> None:
        """
        确认选择落库；失败保留首次选项，重试不会换成另一按钮。
        """
        with self._lock:
            request = self._pending_interactions.get(request_id)
            if request is None or request.user_id != str(user_id):
                return
            if persisted:
                request.selection_persisted = True

    def get_web_choice(
        self, request_id: str, user_id: str, session_id: str,
    ) -> Optional[tuple[PendingAgentInteraction, AgentInteractionOption]]:
        """
        读取已落库的同一用户、同一会话选择，不领取执行。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._pending_interactions.get(request_id)
            if (request is None or request.user_id != str(user_id) or request.session_id != session_id
                    or request.selected_index is None or not request.selection_persisted):
                return None
            return request, request.options[request.selected_index - 1]

    def release_web_choice(self, request_id: str, user_id: str, session_id: str) -> None:
        """
        仅在执行入口明确拒绝接收任务时归还领取，允许原选项重试。
        """
        with self._lock:
            request = self._pending_interactions.get(request_id)
            if request and request.user_id == str(user_id) and request.session_id == session_id:
                request.execution_claimed = False

    def claim_web_choice(
        self, request_id: str, user_id: str, session_id: str,
    ) -> Optional[tuple[PendingAgentInteraction, AgentInteractionOption]]:
        """
        仅为同一用户和会话领取一次已落库选择，重复流请求不再执行业务。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._pending_interactions.get(request_id)
            if (request is None or request.user_id != str(user_id) or request.session_id != session_id
                    or request.selected_index is None or not request.selection_persisted or request.execution_claimed):
                return None
            request.execution_claimed = True
            return request, request.options[request.selected_index - 1]


agent_interaction_manager = AgentInteractionManager()
