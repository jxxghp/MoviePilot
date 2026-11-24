"""
导入正式版本通知配置，生成连接及通知、交互资源和运行态服务投影。
"""

import app.application.messaging.channel as channel_platform
from app.application.messaging.channel.rules import migrate_notification_rules
from app.schemas.channel import (
    AdmissionMode,
    ChannelConnection,
    ChannelMode,
    ChannelPlatformCapabilities,
    ChannelSettings,
    InteractionPolicy,
    NotificationRoute,
    RuleKind,
)
from app.schemas.common import JsonData
from app.schemas.system import NotificationConf


def provider_editable_fields(connection: ChannelConnection) -> tuple[str, ...]:
    """
    消费模块允许编辑的字段，不解释平台身份或凭据。
    """
    platform = channel_platform.platform_catalog().get(connection.provider)
    return platform.editable_fields(connection) if platform else ()


def patch_provider_config(connection: ChannelConnection, patch: dict[str, JsonData]) -> dict[str, JsonData]:
    """
    将私有配置交给所属模块校验；未知类型不开放编辑。
    """
    platform = channel_platform.platform_catalog().get(connection.provider)
    if platform is None:
        if patch:
            raise ValueError("该渠道没有声明可编辑的连接字段")
        return dict(connection.provider_config)
    return platform.patch_config(connection, patch)


def provider_form_values(connection: ChannelConnection) -> dict[str, JsonData]:
    """
    只返回模块明确声明可公开的表单值。
    """
    platform = channel_platform.platform_catalog().get(connection.provider)
    return platform.form_values(connection) if platform else {}


def channel_supports_interaction(provider: str) -> bool:
    """
    目录声明入口能力，不代表调用者拥有交互权限。
    """
    platform = channel_platform.platform_catalog().get(provider)
    return bool(platform and platform.interaction)


def interaction_channel(provider: str | None) -> str | None:
    """
    从既有模块目录解析渠道名称。
    """
    platform = channel_platform.platform_catalog().get(provider or "")
    return platform.channel if platform and platform.interaction else None


def is_managed_interaction_channel(channel: str) -> bool:
    """
    判断渠道是否声明受独立授权保护的交互入口。
    """
    return any(entry.interaction and entry.channel == channel for entry in channel_platform.platform_catalog().providers())


def platform_capabilities(connection: ChannelConnection) -> ChannelPlatformCapabilities:
    """
    缺少模块声明时返回封闭能力，绝不推断授权。
    """
    platform = channel_platform.platform_catalog().get(connection.provider)
    return platform.capabilities(connection) if platform else ChannelPlatformCapabilities()


def supported_rule_kinds(connection: ChannelConnection) -> tuple[RuleKind, ...]:
    """
    公布模块能从已认证事件提取的条件。
    """
    return platform_capabilities(connection).rule_kinds


def supported_admission_modes(connection: ChannelConnection) -> tuple[AdmissionMode, ...]:
    """
    仅为能确认平台身份的连接启用免审批策略。
    """
    if supported_rule_kinds(connection):
        return ("manual", "manual_rules", "rules", "open")
    return ("manual", "open")


def _legacy_mode(provider: str, config: dict[str, JsonData]) -> ChannelMode:
    """
    从旧企业微信配置识别应用或智能机器人，其余渠道使用标准模式。
    """
    platform = channel_platform.platform_catalog().get(provider)
    return platform.mode(config) if platform else "standard"


def import_legacy_channels(configs: list[NotificationConf]) -> ChannelSettings:
    """
    旧配置启用值分别初始化连接、事件通知和受支持的交互。

    :param configs: 当前宿主已经规范化的旧配置记录
    :return: 可用于一次性迁移的渠道资源快照
    :raises ValueError: 重复或缺失的稳定身份不能被迁移为新资源
    """
    connections: dict[str, ChannelConnection] = {}
    notifications: dict[str, NotificationRoute] = {}
    interactions: dict[str, InteractionPolicy] = {}
    for config in configs:
        connection = _legacy_connection(config)
        connection_id = connection.id
        if connection_id in connections:
            raise ValueError("存在重复的旧渠道 ID")
        connections[connection_id] = connection
        notifications[connection_id] = NotificationRoute(
            connection_id=connection_id,
            identity_namespace=connection.identity_namespace,
            enabled=connection.enabled,
            message_types=tuple(dict.fromkeys(switch for switch in config.switchs or () if switch)),
            revision=1,
        )
        if channel_supports_interaction(connection.provider):
            interactions[connection_id] = InteractionPolicy(
                connection_id=connection_id,
                identity_namespace=connection.identity_namespace,
                enabled=connection.enabled,
                revision=1,
            )
    return ChannelSettings(connections=connections, notifications=notifications, interactions=interactions)


def _legacy_connection(config: NotificationConf) -> ChannelConnection:
    """
    一次导入保留旧授权使用的 ID，私有连接参数只进入持久化记录。
    """
    if not config.type or not config.id.strip() or not config.name:
        raise ValueError("旧通知渠道缺少类型、名称或稳定 ID")
    provider = config.type.strip()
    provider_config: dict[str, JsonData] = dict(config.config or {})
    return ChannelConnection(
        id=config.id, name=config.name, provider=provider,
        mode=_legacy_mode(provider, provider_config),
        identity_namespace=config.id, enabled=bool(config.enabled), revision=1,
        provider_config=provider_config,
    )


def project_runtime_channels(settings: ChannelSettings) -> list[NotificationConf]:
    """
    创建供内置模块和插件使用的服务配置投影，连接在启用任一功能时保持活动。

    :param settings: 当前单一配置事实源
    :return: 每个共享连接的一份运行配置
    """
    settings = migrate_notification_rules(settings)
    result = []
    for connection_id, connection in settings.connections.items():
        route = settings.notifications.get(connection_id)
        policy = settings.interactions.get(connection_id)
        active = connection.enabled and connection.credential_status == "ready"
        notify = bool(route and route.enabled)
        interact = bool(policy and policy.enabled)
        platform = channel_platform.platform_catalog().get(connection.provider)
        config = platform.runtime_config(connection, policy) if platform else dict(connection.provider_config)
        result.append(NotificationConf(
            id=connection_id, name=connection.name, type=connection.provider,
            identity_namespace=connection.identity_namespace,
            config=config,
            switchs=list(route.message_types) if route else [],
            enabled=active and (notify or interact),
            notification_enabled=active and notify,
            notification_rules=None if route is None else route.rules,
            interaction_enabled=active and interact,
            interaction_policy=policy,
        ))
    return result


def project_legacy_channels(settings: ChannelSettings) -> list[dict[str, JsonData]]:
    """
    为 Notifications 对外兼容接口提供脱敏服务列表，私有参数由宿主保留。
    """
    return [conf.model_dump(mode="json", exclude={"notification_enabled", "interaction_enabled", "interaction_policy", "notification_rules", "identity_namespace", "config"})
            | {"config": {}} for conf in project_runtime_channels(settings)]
