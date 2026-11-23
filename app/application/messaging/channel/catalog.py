"""
导入正式版本通知配置，生成连接及通知、交互资源和运行态服务投影。
"""

from app.application.messaging.channel.rules import migrate_notification_rules, notification_target_fields
from app.schemas.channel import (
    AdmissionMode,
    ChannelConnection,
    ChannelMode,
    ChannelSettings,
    InteractionPolicy,
    NotificationRoute,
    RuleKind,
)
from app.schemas.common import JsonData
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel

_INTERACTION_CHANNELS = {
    "telegram": NotificationChannel.Telegram,
    "wechat": NotificationChannel.Wechat,
    "wechatclawbot": NotificationChannel.WechatClawBot,
    "feishu": NotificationChannel.Feishu,
    "qqbot": NotificationChannel.QQ,
    "discord": NotificationChannel.Discord,
    "slack": NotificationChannel.Slack,
    "vocechat": NotificationChannel.VoceChat,
    "synologychat": NotificationChannel.SynologyChat,
}
_INTERACTION_CHANNEL_VALUES = frozenset(channel.value for channel in _INTERACTION_CHANNELS.values())


# 只回显投递目标与公开的身份标识，入站信任和认证凭据使用各自的写入边界。
_EDITABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram": ("TELEGRAM_CHAT_ID", "TELEGRAM_TOPIC_ID"),

    "wechat.app": ("WECHAT_TOKEN", "WECHAT_ENCODING_AESKEY"),
    "wechat.bot": ("WECHAT_BOT_CHAT_ID", "WECHAT_BOT_SECRET"),
    "wechatclawbot": ("WECHATCLAWBOT_DEFAULT_TARGET", "WECHATCLAWBOT_POLL_TIMEOUT"),
    "feishu": ("FEISHU_OPEN_ID", "FEISHU_CHAT_ID", "FEISHU_APP_SECRET"),
    "qqbot": ("QQ_OPENID", "QQ_GROUP_OPENID", "QQ_APP_SECRET"),
    "discord": (),
    "slack": ("SLACK_CHANNEL",),
    "vocechat": ("VOCECHAT_CHANNEL_ID",),
    "synologychat": ("SYNOLOGYCHAT_WEBHOOK",),
    "dingtalk": ("DINGTALK_WEBHOOK", "DINGTALK_SECRET"),
    "webpush": ("WEBPUSH_USERNAME",),
    "apppush": ("PUSH_SERVER_URL", "PUSH_RELAY_KEY"),
}
_WRITE_ONLY_FIELDS = frozenset({
    "DINGTALK_WEBHOOK", "DINGTALK_SECRET", "PUSH_RELAY_KEY", "WECHAT_BOT_SECRET",
    "WECHAT_TOKEN", "WECHAT_ENCODING_AESKEY", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY", "QQ_APP_SECRET", "SYNOLOGYCHAT_WEBHOOK",
})
_PUBLIC_ID_FIELDS = frozenset({
    "WECHAT_APP_ID", "WECHAT_CORPID", "WECHAT_BOT_ID", "FEISHU_APP_ID", "QQ_APP_ID",
    "WECHAT_PROXY", "WECHAT_BOT_WS_URL", "WECHATCLAWBOT_BASE_URL", "VOCECHAT_HOST", "API_URL",
})


def provider_editable_fields(connection: ChannelConnection) -> tuple[str, ...]:
    """
    返回可以在保留原平台身份时修改的字段。
    """
    key = f"wechat.{connection.mode}" if connection.provider == "wechat" else connection.provider
    fields = _EDITABLE_FIELDS.get(key, ())
    if key == "wechat.bot" and connection.provider_config.get("WECHAT_BOT_WS_URL") not in (
        None, "", "wss://openws.work.weixin.qq.com",
    ):
        return ()
    return tuple(field for field in fields if field not in notification_target_fields(connection.provider))


def patch_provider_config(connection: ChannelConnection, patch: dict[str, JsonData]) -> dict[str, JsonData]:
    """
    合并允许编辑的值。敏感输入留空时保留当前值。
    """
    editable = provider_editable_fields(connection)
    if patch.keys() - set(editable):
        raise ValueError("连接平台身份及受保护字段需通过凭据轮换或新建连接修改")
    return connection.provider_config | {
        key: value for key, value in patch.items()
        if key not in _WRITE_ONLY_FIELDS or value not in (None, "")
    }


def provider_form_values(connection: ChannelConnection) -> dict[str, JsonData]:
    """
    返回平台编辑器可回填的值。
    """
    visible = set(provider_editable_fields(connection)) - _WRITE_ONLY_FIELDS
    visible.update(_PUBLIC_ID_FIELDS)
    values = {key: value for key, value in connection.provider_config.items() if key in visible}
    if connection.provider == "wechat":
        values["WECHAT_MODE"] = connection.mode
    return values


def channel_supports_interaction(provider: str) -> bool:
    """
    依据宿主内置目录判断该渠道是否提供机器人入口。
    """
    return provider in _INTERACTION_CHANNELS


def interaction_channel(provider: str | None) -> str | None:
    """
    从当前宿主目录解析连接所属的机器人交互渠道。

    :param provider: 连接的平台标识
    :return: 支持交互的渠道，无入站能力时为空
    """
    channel = _INTERACTION_CHANNELS.get(provider or "")
    return channel.value if channel else None


def is_managed_interaction_channel(channel: str) -> bool:
    """
    判断持久化渠道名称是否属于需要独立授权身份的机器人入口。

    :param channel: 已存储的渠道显示值
    :return: 该渠道是否受机器人交互目录管理
    """
    return channel in _INTERACTION_CHANNEL_VALUES


def supported_rule_kinds(connection: ChannelConnection) -> tuple[RuleKind, ...]:
    """
    公布适配器能从已认证事件提取的条件。
    """
    config = connection.provider_config
    if connection.provider == "wechatclawbot" and config.get("WECHATCLAWBOT_BASE_URL") not in (
        None, "", "https://ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com/",
    ):
        return ()
    if connection.provider == "telegram" and config.get("API_URL") not in (
        None, "", "https://api.telegram.org", "https://api.telegram.org/",
    ):
        return ()
    if connection.provider == "synologychat":
        return ("user",) if config.get("SYNOLOGYCHAT_TOKEN") else ()
    if connection.provider == "vocechat":
        return ("user", "group", "private") if config.get("VOCECHAT_INBOUND_TOKEN") else ()
    if connection.provider in _SDK_RULE_KINDS:
        return _SDK_RULE_KINDS[connection.provider]
    if connection.provider != "wechat":
        return ()
    if connection.mode == "bot" and config.get("WECHAT_BOT_ID") and config.get("WECHAT_BOT_SECRET") and (
        not config.get("WECHAT_BOT_WS_URL") or config["WECHAT_BOT_WS_URL"] == "wss://openws.work.weixin.qq.com"
    ):
        return ("user", "group", "private")
    if connection.mode == "app" and all(config.get(key) for key in (
        "WECHAT_CORPID", "WECHAT_APP_ID", "WECHAT_ENCODING_AESKEY", "WECHAT_TOKEN",
    )):
        return ("user", "private")
    return ()


_SDK_RULE_KINDS: dict[str, tuple[RuleKind, ...]] = {
    "telegram": ("user", "group", "private"),
    "qqbot": ("group", "user"),
    "feishu": ("user", "group", "private"),
    "discord": ("user", "group", "private"),
    "slack": ("user", "group", "private"),
    "wechatclawbot": ("user",),
}


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
    if provider == "wechat":
        return "bot" if config.get("WECHAT_MODE") == "bot" else "app"
    return "standard"


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
        config = dict(connection.provider_config)
        if connection.provider == "discord" and policy and policy.command_guild_id:
            config["DISCORD_COMMAND_GUILD_ID"] = policy.command_guild_id
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
