import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from queue import Queue
from threading import Lock
from typing import Dict, List, Optional, Tuple, Union

from app.runtime.channels import (  # noqa: F401  渠道管理员判定的对外导出
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)

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
) -> Tuple[List[dict], List[List[dict]]]:
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


_WEB_AGENT_EDIT_QUEUES: dict[str, list[Queue[dict]]] = {}
_WEB_AGENT_EDIT_LOCK = Lock()


def normalize_web_agent_button_rows(buttons: Optional[list[list[dict]]]) -> list[list[dict]]:
    """
    将消息按钮转换为 WebAgent 前端可识别的按钮行。

    :param buttons: 传统消息模块返回的按钮二维数组
    :return: WebAgent 前端选项按钮二维数组
    """
    button_rows: list[list[dict]] = []
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
        button_rows: list[list[dict]],
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
        buttons: Optional[list[list[dict]]],
) -> dict:
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
        target_message["choices"].append({
            "id": _resolve_web_agent_choice_id(message_id, button_rows),
            "title": title,
            "prompt": text or "",
            "buttons": [button for row in button_rows for button in row],
            "button_rows": button_rows,
            "status": "pending",
        })
    return {
        "type": "message_update",
        "target_message": target_message,
    }


def attach_web_agent_edit_queue(user_id: str, edit_queue: Queue[dict]) -> None:
    """
    为当前 WebAgent 请求挂载原消息编辑事件队列。

    :param user_id: 当前用户 ID
    :param edit_queue: 用于接收编辑事件的队列
    """
    with _WEB_AGENT_EDIT_LOCK:
        _WEB_AGENT_EDIT_QUEUES.setdefault(str(user_id), []).append(edit_queue)


def detach_web_agent_edit_queue(user_id: str, edit_queue: Queue[dict]) -> None:
    """
    移除当前 WebAgent 请求的原消息编辑事件队列。

    :param user_id: 当前用户 ID
    :param edit_queue: 需要移除的队列
    """
    with _WEB_AGENT_EDIT_LOCK:
        queues = _WEB_AGENT_EDIT_QUEUES.get(str(user_id))
        if not queues:
            return
        _WEB_AGENT_EDIT_QUEUES[str(user_id)] = [
            item for item in queues if item is not edit_queue
        ]
        if not _WEB_AGENT_EDIT_QUEUES[str(user_id)]:
            _WEB_AGENT_EDIT_QUEUES.pop(str(user_id), None)


def dispatch_web_agent_edit_event(
        *,
        user_id: str,
        event: dict,
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
        buttons: Optional[list[list[dict]]] = None,
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
