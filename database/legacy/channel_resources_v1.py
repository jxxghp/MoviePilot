"""
62c91ad8b057 的固定离线配置导入；保留升级时的格式与权限处理，不解析当前模块声明。
"""

import re
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import JsonData
from database.legacy.channel_resources_v1_schema import (
    ChannelConnection,
    ChannelMode,
    ChannelSettings,
    InteractionPolicy,
    NotificationRoute,
    NotificationRule,
    NotificationTargetKind,
)


class NotificationConf(BaseModel):
    """
    正式版本通知记录在此迁移中实际使用的固定字段。
    """

    id: str = Field(description="历史稳定连接 ID")
    name: str | None = Field(default=None, description="历史显示名称")
    type: str | None = Field(default=None, description="历史平台标识")
    enabled: bool = Field(default=False, description="历史连接开关")
    switchs: list[str] | None = Field(default=None, description="历史通知事件类型")
    config: dict[str, JsonData] | None = Field(default=None, description="历史平台配置")


_DEFAULTS: dict[str, tuple[tuple[str, NotificationTargetKind], ...]] = {
    "telegram": (("TELEGRAM_CHAT_ID", "conversation"),),
    "wechat": (("WECHAT_BOT_CHAT_ID", "user"),),
    "wechatclawbot": (("WECHATCLAWBOT_DEFAULT_TARGET", "user"),),
    "feishu": (("FEISHU_OPEN_ID", "user"), ("FEISHU_CHAT_ID", "conversation")),
    "qqbot": (("QQ_GROUP_OPENID", "conversation"), ("QQ_OPENID", "user")),
    "discord": (("DISCORD_CHANNEL_ID", "conversation"),),
    "slack": (("SLACK_CHANNEL", "conversation"),),
    "vocechat": (("VOCECHAT_CHANNEL_ID", "conversation"),),
}

_TARGET_KINDS: dict[str, tuple[NotificationTargetKind, ...]] = {
    "telegram": ("bound", "user", "conversation"),
    "wechat.app": ("bound", "user", "all"),
    "wechat.bot": ("bound", "user", "conversation"),
    "wechatclawbot": ("bound", "user"),
    "feishu": ("bound", "user", "conversation"),
    "qqbot": ("bound", "user", "conversation"),
    "discord": ("bound", "user", "conversation"),
    "slack": ("bound", "user", "conversation"),
    "vocechat": ("bound", "user", "conversation"),
    "synologychat": ("bound", "user", "all"),
    "dingtalk": ("webhook",),
    "webpush": ("bound", "account"),
    "apppush": ("bound", "account"),
}


def notification_target_kinds(connection: ChannelConnection) -> tuple[NotificationTargetKind, ...]:
    """
    返回指定平台模式支持的主动通知目标类型。
    """
    provider = connection.provider
    return _TARGET_KINDS.get(f"wechat.{connection.mode}" if provider == "wechat" else provider, ())


def notification_rule_types(rules: tuple[NotificationRule, ...]) -> tuple[str, ...]:
    """
    为兼容旧查询按原顺序投影规则中使用的事件类型。
    """
    return tuple(dict.fromkeys(kind for rule in rules for kind in rule.message_types))


def notification_target_fields(provider: str) -> frozenset[str]:
    """
    返回应从平台连接配置移交给投递规则的字段。
    """
    pending = {"telegram": "TELEGRAM_TOPIC_ID", "webpush": "WEBPUSH_USERNAME"}
    return frozenset(key for key, _ in _DEFAULTS.get(provider, ())) | (
        {pending[provider]} if provider in pending else set()
    )


def _text(value: JsonData) -> str:
    """
    保留数字和文本配置中的平台地址。
    """
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def validate_notification_rule(connection: ChannelConnection, rule: NotificationRule) -> None:
    """
    检查平台地址格式及此连接的投递能力。
    """
    provider = connection.provider
    if rule.kind == "review":
        return
    if rule.kind not in notification_target_kinds(connection):
        raise ValueError("该渠道不支持所选投递目标")
    if rule.owner_identity and provider not in {"webpush", "apppush"}:
        raise ValueError("该渠道不支持按关联账号筛选事件")
    if rule.legacy_usernames and provider != "webpush":
        raise ValueError("只有 Web Push 可使用旧用户名条件")
    if rule.fallback_only and rule.kind in {"bound", "account", "webhook"}:
        raise ValueError("仅明确地址或平台全体目标可作为候补")
    if rule.thread_id and (provider != "telegram" or not re.fullmatch(r"[1-9][0-9]*", rule.thread_id)):
        raise ValueError("目前仅 Telegram 会话支持正整数话题 ID")
    _validate_target_address(connection, rule)


def _validate_target_address(connection: ChannelConnection, rule: NotificationRule) -> None:
    """
    按平台协议校验已通过能力检查的投递地址。
    """
    provider = connection.provider
    address = rule.target
    if address is None:
        return
    if provider in {"telegram", "discord", "synologychat", "vocechat"}:
        pattern = r"-?[1-9][0-9]*" if provider == "telegram" and rule.kind == "conversation" else r"[1-9][0-9]*"
        if not re.fullmatch(pattern, address):
            raise ValueError("请输入有效的平台数字 ID")
    if provider == "wechat" and (address.lower() == "@all" or not re.fullmatch(r"[A-Za-z0-9_@.-]+", address)):
        raise ValueError("请输入单个企业微信用户或群组 ID")
    if provider == "qqbot" and (":" in address or any(char.isspace() for char in address)):
        raise ValueError("请输入原始 QQ OpenID")
    if provider == "feishu":
        prefix = "oc_" if rule.kind == "conversation" else "ou_"
        if not address.startswith(prefix):
            raise ValueError("请输入飞书对应类型的 ID")
    if provider == "slack":
        pattern = r"[UW][A-Z0-9]+" if rule.kind == "user" else r"[CGD][A-Z0-9]+|#[^\s]+"
        if not re.fullmatch(pattern, address):
            raise ValueError("请输入 Slack 用户或频道 ID")


def _legacy_addresses(connection: ChannelConnection) -> Iterator[tuple[NotificationTargetKind, str, str | None]]:
    """
    按各适配器原有优先级读取默认地址，较低优先级地址可供日后手动启用。
    """
    provider = connection.provider
    config = connection.provider_config
    for key, kind in _DEFAULTS.get(provider, ()):
        address = _text(config.get(key))
        if not address or (provider == "wechat" and connection.mode != "bot"):
            continue
        if provider == "wechat":
            if address.lower().startswith("group:"):
                kind = "conversation"
            if address.lower().startswith(("group:", "user:")):
                address = address.split(":", 1)[1].strip()
        topic = _text(config.get("TELEGRAM_TOPIC_ID")) if provider == "telegram" else ""
        if topic.isascii() and topic.isdecimal() and len(topic) < 16 and int(topic) > 0:
            topic = str(int(topic))
        else:
            topic = ""
        yield kind, address, topic or None


def _legacy_default_rule(
    connection: ChannelConnection,
    types: tuple[str, ...],
    address: tuple[NotificationTargetKind, str, str | None],
    *,
    enabled: bool,
) -> NotificationRule:
    """
    转换可确认地址，保留待确认标记以避免启用未知目标。
    """
    kind, target, thread_id = address
    try:
        rule = NotificationRule(
            kind=kind,
            message_types=types,
            target=target,
            thread_id=thread_id,
            fallback_only=True,
            enabled=enabled,
        )
        validate_notification_rule(connection, rule)
    except ValueError:
        return NotificationRule(kind="review", message_types=types, enabled=False)
    return rule


def _legacy_rules(connection: ChannelConnection, route: NotificationRoute) -> tuple[NotificationRule, ...]:
    """
    保留已绑定个人地址及已明确的默认地址。动态回退留给管理员确认。
    """
    types = route.message_types
    capabilities = notification_target_kinds(connection)
    addresses = tuple(_legacy_addresses(connection))
    raw_names = connection.provider_config.get("WEBPUSH_USERNAME") if connection.provider == "webpush" else None
    if not types and not addresses and not raw_names:
        return ()
    if "webhook" in capabilities:
        return (NotificationRule(kind="webhook", message_types=types),)
    usernames = raw_names if isinstance(raw_names, str) else ""
    names = tuple(
        dict.fromkeys(name for name in usernames.split(",") if name and name == name.strip() and len(name) <= 160)
    )
    rules = [
        NotificationRule(
            kind="bound",
            message_types=types,
            legacy_usernames=names[:100],
            enabled=bool(types) and (not raw_names or bool(names)),
        )
    ]
    if addresses:
        rules.extend(
            _legacy_default_rule(connection, types, address, enabled=bool(types) and index == 0)
            for index, address in enumerate(addresses)
        )
    elif "all" in capabilities and types:
        rules.append(NotificationRule(kind="all", message_types=types, audience="all", fallback_only=True))
    elif (
        connection.provider in {"qqbot", "feishu", "discord", "slack", "wechat", "wechatclawbot"}
        or raw_names
        and not names
    ):
        rules.append(NotificationRule(kind="review", message_types=types, enabled=False))
    return tuple(rules)


def migrate_notification_rules(settings: ChannelSettings) -> ChannelSettings:
    """
    幂等导入旧通知类型和目标，并移交连接字段。
    """
    for key, route in settings.notifications.items():
        connection = settings.connections[key]
        if route.rules is not None or not notification_target_kinds(connection):
            continue
        rules = _legacy_rules(connection, route)
        settings.notifications[key] = route.model_copy(update={"rules": rules})
        obsolete = notification_target_fields(connection.provider)
        if connection.provider == "discord" and key in settings.interactions:
            policy = settings.interactions[key]
            guild = _text(connection.provider_config.get("DISCORD_GUILD_ID"))
            if guild and re.fullmatch(r"[1-9][0-9]*", guild) and not policy.platform_options.get("command_guild_id"):
                settings.interactions[key] = policy.model_copy(update={"platform_options": {"command_guild_id": guild}})
            obsolete |= {"DISCORD_GUILD_ID"}
        if connection.provider == "feishu":
            obsolete |= {"FEISHU_VERIFICATION_TOKEN", "FEISHU_ENCRYPT_KEY"}
        provider_config = {field: value for field, value in connection.provider_config.items() if field not in obsolete}
        settings.connections[key] = connection.model_copy(update={"provider_config": provider_config})
    return settings


_INTERACTION_CHANNELS = frozenset(
    {"telegram", "wechat", "wechatclawbot", "feishu", "qqbot", "discord", "slack", "vocechat", "synologychat"}
)


def channel_supports_interaction(provider: str) -> bool:
    """
    依据宿主内置目录判断该渠道是否提供机器人入口。
    """
    return provider in _INTERACTION_CHANNELS


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
        id=config.id,
        name=config.name,
        provider=provider,
        mode=_legacy_mode(provider, provider_config),
        identity_namespace=config.id,
        enabled=bool(config.enabled),
        revision=1,
        provider_config=provider_config,
    )


def read_channel_resources(raw: Any, legacy_rows: Any) -> ChannelSettings:
    """
    在第一次专用保存前从旧数据生成只读视图，已有新事实源时使用该快照。

    :param raw: 已持久化的渠道资源，尚未初始化时为 None
    :param legacy_rows: 旧通知配置
    :return: 供查询或事务起点使用的资源快照
    """
    if raw is not None:
        return migrate_notification_rules(ChannelSettings.model_validate(raw))
    return migrate_notification_rules(import_legacy_channels(_parse_legacy_rows(legacy_rows)))


def _parse_legacy_rows(legacy_rows: Any) -> list[NotificationConf]:
    """
    将旧配置规范化为稳定 ID，保留重复 ID 供修复入口核对。
    """
    records = []
    for row in legacy_rows or []:
        if not isinstance(row, dict):
            raise ValueError("旧通知配置必须是对象列表")
        normalized = dict(row)
        if not normalized.get("id"):
            normalized["id"] = f"legacy-{row.get('type') or 'notification'}-{row.get('name') or 'unnamed'}"
        records.append(NotificationConf.model_validate(normalized))
    return records
