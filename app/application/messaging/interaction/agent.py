"""Agent 按钮选择交互契约与进程内待处理请求管理。"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Optional

# Agent 选择按钮回调前缀，新旧两种格式都必须继续兼容。
AGENT_CHOICE_PREFIX = "agent_interaction:choice:"
LEGACY_AGENT_CHOICE_PREFIX = "agent_choice:"


def build_agent_choice_callback(request_id: str, option_index: int) -> str:
    """构造 Agent 选择按钮回调数据。"""
    return f"{AGENT_CHOICE_PREFIX}{request_id}:{option_index}"


def parse_agent_choice_callback(
    callback_data: str,
) -> Optional[tuple[str, int]]:
    """解析新旧两种 Agent 选择回调，格式无效时返回 None。"""
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
    """Agent 交互选项。"""

    label: str
    value: str
    description: Optional[str] = None


@dataclass
class PendingAgentInteraction:
    """待处理的 Agent 客户端交互请求。"""

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


def build_agent_choice_button_rows(
    request: PendingAgentInteraction,
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
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


class AgentInteractionManager:
    """管理 Agent 发起的客户端交互请求。"""

    _ttl = timedelta(hours=24)

    def __init__(self) -> None:
        """初始化待处理的 Agent 交互请求表。"""
        self._pending_interactions: dict[str, PendingAgentInteraction] = {}
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
        options: list[AgentInteractionOption],
    ) -> PendingAgentInteraction:
        """创建一条待用户确认的 Agent 交互请求。"""
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
        """消费一条 Agent 交互请求，并返回选中的选项。"""
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
        """清空所有 Agent 交互请求。"""
        with self._lock:
            self._pending_interactions.clear()


agent_interaction_manager = AgentInteractionManager()
