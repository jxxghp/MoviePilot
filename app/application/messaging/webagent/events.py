"""WebAgent 通知与原消息编辑事件桥接。"""

import copy
from queue import Queue
from threading import Lock
from typing import Any, Optional, Union

from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.types import NotificationChannel

_WEB_AGENT_EDIT_QUEUES: dict[str, list[Queue[dict[str, Any]]]] = {}
_WEB_AGENT_EDIT_LOCK = Lock()
_WEB_AGENT_MESSAGE_QUEUES: dict[str, list[Queue[Message]]] = {}
_WEB_AGENT_MESSAGE_LOCK = Lock()


def normalize_web_agent_button_rows(
    buttons: Optional[list[list[dict[str, Any]]]],
) -> list[list[dict[str, Any]]]:
    """将消息按钮转换为 WebAgent 前端可识别的按钮行。"""
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
    """从按钮回调中提取稳定的 WebAgent 选项 ID。"""
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
    """构造可应用到 WebAgent 原消息的 SSE 更新事件。"""
    button_rows = normalize_web_agent_button_rows(buttons)
    content_parts = [part for part in (title, text) if part]
    target_message: dict[str, Any] = {
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


def extract_web_agent_message_from_event_data(
    data: dict[str, Any],
) -> Optional[Message]:
    """从兼容新旧结构的 NoticeMessage 事件数据中提取 WebAgent 通知。"""
    if not isinstance(data, dict):
        return None
    try:
        message = data.get("message")
        if isinstance(message, Message):
            resolved_message = message
        elif isinstance(message, dict):
            message_data = copy.deepcopy(message)
            message_data.pop("type", None)
            resolved_message = Message(**message_data)
        else:
            message_data = copy.deepcopy(data)
            message_data.pop("type", None)
            message_data.pop("current_time", None)
            resolved_message = Message(**message_data)
    except Exception as err:
        logger.debug(f"解析WebAgent通知事件失败: {err}")
        return None

    channel = resolved_message.channel
    channel_value = (
        channel.value if isinstance(channel, NotificationChannel) else channel
    )
    if channel_value != NotificationChannel.WebAgent.value:
        return None
    return resolved_message


def is_web_agent_message_for_user(message: Message, user_id: str) -> bool:
    """判断 NoticeMessage 事件是否属于当前 WebAgent 用户。"""
    try:
        target_user = message.userid
        return target_user is None or str(target_user) == str(user_id)
    except Exception:
        return False


def _get_web_agent_message_user_id(message: Message) -> Optional[str]:
    """返回 WebAgent 通知的目标用户 ID，无目标时返回 None。"""
    try:
        channel = message.channel
        channel_value = (
            channel.value if isinstance(channel, NotificationChannel) else channel
        )
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
                message_queue
                for user_queues in _WEB_AGENT_MESSAGE_QUEUES.values()
                for message_queue in user_queues
            ]
        else:
            queues = list(_WEB_AGENT_MESSAGE_QUEUES.get(user_id) or [])
    for message_queue in queues:
        message_queue.put(message)


def attach_web_agent_message_queue(
    user_id: str,
    message_queue: Queue[Message],
) -> None:
    """为当前 WebAgent 请求挂载通知收集队列。"""
    with _WEB_AGENT_MESSAGE_LOCK:
        _WEB_AGENT_MESSAGE_QUEUES.setdefault(str(user_id), []).append(message_queue)


def detach_web_agent_message_queue(
    user_id: str,
    message_queue: Queue[Message],
) -> None:
    """移除当前 WebAgent 请求的通知收集队列。"""
    with _WEB_AGENT_MESSAGE_LOCK:
        queues = _WEB_AGENT_MESSAGE_QUEUES.get(str(user_id))
        if not queues:
            return
        _WEB_AGENT_MESSAGE_QUEUES[str(user_id)] = [
            item for item in queues if item is not message_queue
        ]
        if not _WEB_AGENT_MESSAGE_QUEUES[str(user_id)]:
            _WEB_AGENT_MESSAGE_QUEUES.pop(str(user_id), None)


def attach_web_agent_edit_queue(
    user_id: str,
    edit_queue: Queue[dict[str, Any]],
) -> None:
    """为当前 WebAgent 请求挂载原消息编辑事件队列。"""
    with _WEB_AGENT_EDIT_LOCK:
        _WEB_AGENT_EDIT_QUEUES.setdefault(str(user_id), []).append(edit_queue)


def detach_web_agent_edit_queue(
    user_id: str,
    edit_queue: Queue[dict[str, Any]],
) -> None:
    """移除当前 WebAgent 请求的原消息编辑事件队列。"""
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
    event: dict[str, Any],
) -> bool:
    """将 WebAgent 原消息编辑事件分发给正在等待的请求队列。"""
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
    """构造并投递 WebAgent 原消息更新事件。"""
    if not user_id:
        return False
    event = build_web_agent_message_update_event(
        message_id=message_id,
        title=title,
        text=text,
        buttons=buttons,
    )
    return dispatch_web_agent_edit_event(user_id=user_id, event=event)
