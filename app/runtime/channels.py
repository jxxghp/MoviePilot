"""消息渠道管理员主体的解析注册表与判定。"""

from typing import Callable, Iterable, Optional, Union

from app.schemas.notification import ChannelRef, channel_identity


_ChannelAdminResolver = Callable[[Optional[dict]], Iterable[Union[str, int]]]
_CHANNEL_ADMIN_RESOLVERS: dict[str, _ChannelAdminResolver] = {}


def register_channel_admin_resolver(
        channel: ChannelRef,
        resolver: _ChannelAdminResolver,
) -> None:
    """
    注册消息渠道的管理员主体 ID 解析器。

    :param channel: 消息渠道
    :param resolver: 由渠道配置解析全部管理员主体 ID 的函数
    """
    identity = channel_identity(channel)
    if not identity:
        raise ValueError("消息渠道标识不能为空")
    _CHANNEL_ADMIN_RESOLVERS[identity] = resolver


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
        channel: Optional[ChannelRef],
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
    resolver = _CHANNEL_ADMIN_RESOLVERS.get(channel_identity(channel))
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
