from queue import Queue
from threading import Lock
from typing import Callable, Iterable, Optional, Union

from app.schemas.types import MessageChannel


_WEB_AGENT_EDIT_QUEUES: dict[str, list[Queue[dict]]] = {}
_WEB_AGENT_EDIT_LOCK = Lock()
_ChannelAdminResolver = Callable[[Optional[dict]], Iterable[Union[str, int]]]
_CHANNEL_ADMIN_RESOLVERS: dict[str, _ChannelAdminResolver] = {}


def register_channel_admin_resolver(
        channel: Union[MessageChannel, str],
        resolver: _ChannelAdminResolver,
) -> None:
    """
    注册消息渠道的管理员主体 ID 解析器。

    :param channel: 消息渠道
    :param resolver: 由渠道配置解析全部管理员主体 ID 的函数
    """
    channel_value = channel.value if isinstance(channel, MessageChannel) else str(channel)
    _CHANNEL_ADMIN_RESOLVERS[channel_value] = resolver


def resolve_config_principal_ids(
        config: Optional[dict],
        *config_keys: str,
) -> set[str]:
    """
    从渠道自行声明的配置键中解析主体 ID。

    :param config: 当前消息渠道配置
    :param config_keys: 由渠道模块维护的主体 ID 配置键
    :return: 去空白后的主体 ID 集合
    """
    principal_ids = set()
    for config_key in config_keys:
        principal_ids.update(
            item.strip()
            for item in str((config or {}).get(config_key) or "").split(",")
            if item.strip()
        )
    return principal_ids


def matches_channel_admin(
        channel: Union[MessageChannel, str],
        config: Optional[dict],
        *principal_ids: Optional[Union[str, int]],
) -> bool:
    """
    按渠道配置中的稳定主体 ID 判断管理员身份。

    :param channel: 消息渠道
    :param config: 当前消息渠道配置
    :param principal_ids: 消息渠道提供的稳定用户主体 ID
    :return: 任一用户主体 ID 命中渠道注册的管理员集合时返回 True
    """
    channel_value = channel.value if isinstance(channel, MessageChannel) else str(channel)
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
