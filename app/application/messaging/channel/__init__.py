"""
渠道模块契约的精确懒门面；冷导入不解析平台实现，业务逻辑由 platform owner 持有。
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.application.messaging.channel.platform import (
        ChannelPlatform,
        ChannelPlatformCatalog,
        normalize_policy_actors,
        platform_catalog,
        platform_for_channel,
        resolve_actor_ids,
    )

_EXPORTS = {'ChannelPlatform': 'app.application.messaging.channel.platform', 'ChannelPlatformCatalog': 'app.application.messaging.channel.platform', 'platform_catalog': 'app.application.messaging.channel.platform', 'platform_for_channel': 'app.application.messaging.channel.platform', 'resolve_actor_ids': 'app.application.messaging.channel.platform', 'normalize_policy_actors': 'app.application.messaging.channel.platform'}
__all__ = ['ChannelPlatform', 'ChannelPlatformCatalog', 'platform_catalog', 'platform_for_channel', 'resolve_actor_ids', 'normalize_policy_actors']


def __getattr__(name: str) -> Any:
    """
    按明确白名单惰性解析应用端口，未知符号不能变成通用 service locator。

    :param name: 明确公开的渠道契约符号
    :return: 对应职责 owner 的原对象
    :raises AttributeError: 名称不在契约白名单
    """
    owner = _EXPORTS.get(name)
    if owner is None:
        raise AttributeError(name)
    return getattr(import_module(owner), name)
