"""插件渠道能力声明的契约校验。

渠道能力声明直接复用 ``app.schemas.notification.ChannelCapabilities``：该
dataclass 本身已是纯数据（渠道标识、能力集合、渲染限制），没有需要与序列化
边界隔开的可执行实现，不必再包一层声明壳提供 ``impl`` 字段。
"""

from __future__ import annotations

from typing import Any, Optional

from app.schemas.notification import ChannelCapabilities, ChannelCapability, channel_identity


def channel_capability_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验渠道能力声明是否满足登记契约

    契约要求声明是 ``ChannelCapabilities`` 实例、渠道标识非空、能力集合是
    ``ChannelCapability`` 成员的可迭代集合；三项中任一不满足都拒绝登记，
    不留到调用时才失败。

    :param declaration: 渠道能力声明
    :return: 违反契约的描述；声明合规时为 None
    """
    if not isinstance(declaration, ChannelCapabilities):
        return f"{declaration!r} 不是 ChannelCapabilities 声明"
    try:
        identity = channel_identity(declaration.channel)
    except Exception as error:
        return f"读取渠道标识出错：{error}"
    if not identity:
        return "渠道标识 channel 缺失或为空"
    capabilities = declaration.capabilities
    if not isinstance(capabilities, (set, frozenset, list, tuple)):
        return f"capabilities 必须是能力集合，实际是 {type(capabilities).__name__}"
    invalid = [item for item in capabilities if not isinstance(item, ChannelCapability)]
    if invalid:
        return f"capabilities 含非 ChannelCapability 成员：{invalid!r}"
    return None
