"""
消息渠道管理员主体解析与匹配契约。
"""

from collections.abc import Callable, Iterable
from typing import Any, Optional, Union

from app.schemas.types import NotificationChannel

ChannelAdminResolver = Callable[
    [Optional[dict[str, Any]]],
    Iterable[Union[str, int]],
]
_CHANNEL_ADMIN_RESOLVERS: dict[str, ChannelAdminResolver] = {}


def register_channel_admin_resolver(
    channel: Union[NotificationChannel, str],
    resolver: ChannelAdminResolver,
) -> None:
    """
    注册消息渠道的管理员主体 ID 解析器。
    """
    channel_value = (
        channel.value if isinstance(channel, NotificationChannel) else str(channel)
    )
    _CHANNEL_ADMIN_RESOLVERS[channel_value] = resolver


def resolve_config_principal_ids(
    config: Optional[dict[str, Any]],
    *config_keys: str,
) -> set[str]:
    """
    从渠道自行声明的配置键中解析并清理主体 ID。
    """
    principal_ids: set[str] = set()
    for config_key in config_keys:
        principal_ids.update(
            item.strip()
            for item in str((config or {}).get(config_key) or "").split(",")
            if item.strip()
        )
    return principal_ids


def matches_channel_admin(
    channel: Union[NotificationChannel, str],
    config: Optional[dict[str, Any]],
    *principal_ids: Optional[Union[str, int]],
    source: str | None = None,
) -> bool:
    """
    判断任一稳定主体 ID 是否命中渠道登记的管理员集合。
    """
    channel_value = (
        channel.value if isinstance(channel, NotificationChannel) else str(channel)
    )
    resolver = _CHANNEL_ADMIN_RESOLVERS.get(channel_value)
    # 已装配时统一授权是唯一事实源，不能通过旧名单或接收人绕过撤销。
    from app.application.security.authorization import get_channel_authorization

    service = get_channel_authorization()
    if service is not None:
        if not source:
            from app.application.notification import get_notification_configs

            matches = [item.name for item in get_notification_configs() if item.config == config]
            source = matches[0] if len(matches) == 1 else None
        return bool(source) and any(
            service.is_admin(channel_value, source or "", str(principal_id).strip())
            for principal_id in principal_ids if principal_id is not None
        )
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
