"""
内置渠道投递目的地、旧配置迁移及主动事件匹配。
"""

import re
from collections.abc import Iterator

from app.application.messaging.delivery import (
    notification_rule_time_allows,
    notification_rules_are_frozen,
    notification_sending_windows,
)
from app.schemas.channel import (
    ChannelConnection,
    ChannelSettings,
    NotificationRoute,
    NotificationRule,
    NotificationTargetKind,
)
from app.schemas.common import JsonData
from app.schemas.message import Message
from app.schemas.system import NotificationConf

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
    connection: ChannelConnection, types: tuple[str, ...],
    address: tuple[NotificationTargetKind, str, str | None], *, enabled: bool,
) -> NotificationRule:
    """
    转换可确认地址，保留待确认标记以避免启用未知目标。
    """
    kind, target, thread_id = address
    try:
        rule = NotificationRule(
            kind=kind, message_types=types, target=target, thread_id=thread_id, fallback_only=True, enabled=enabled,
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
    names = tuple(dict.fromkeys(
        name for name in usernames.split(",") if name and name == name.strip() and len(name) <= 160
    ))
    rules = [NotificationRule(
        kind="bound", message_types=types, legacy_usernames=names[:100], enabled=bool(types) and (not raw_names or bool(names)),
    )]
    if addresses:
        rules.extend(
            _legacy_default_rule(connection, types, address, enabled=bool(types) and index == 0)
            for index, address in enumerate(addresses)
        )
    elif "all" in capabilities and types:
        rules.append(NotificationRule(kind="all", message_types=types, audience="all", fallback_only=True))
    elif connection.provider in {"qqbot", "feishu", "discord", "slack", "wechat", "wechatclawbot"} or raw_names and not names:
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
            if guild and re.fullmatch(r"[1-9][0-9]*", guild) and not policy.command_guild_id:
                settings.interactions[key] = policy.model_copy(update={"command_guild_id": guild})
            obsolete |= {"DISCORD_GUILD_ID"}
        if connection.provider == "feishu":
            obsolete |= {"FEISHU_VERIFICATION_TOKEN", "FEISHU_ENCRYPT_KEY"}
        provider_config = {field: value for field, value in connection.provider_config.items() if field not in obsolete}
        settings.connections[key] = connection.model_copy(update={"provider_config": provider_config})
    return settings


def matches_notification_rule(message: Message, rule: NotificationRule, *, check_time: bool = True) -> bool:
    """
    仅对启用的规则检查事件条件及宿主已确认的通知范围。
    """
    return (
        rule.enabled and (not check_time or notification_rule_time_allows(rule))
        and message.mtype is not None and message.mtype.value in rule.message_types
        and (not rule.matched_audiences or message.notification_route_scope in rule.matched_audiences)
        and (not rule.legacy_usernames or message.username in rule.legacy_usernames)
        and (not rule.owner_identity or rule.owner_identity == message.notification_owner_identity)
    )


def notification_rule_applies(message: Message) -> bool:
    """
    显式发送、回复、回执和任务继续使用其用途的目标。
    """
    return (
        message.delivery_kind in {None, "event_notification"}
        and not (message.private_delivery or message.userid or message.original_chat_id or message.original_message_id)
        and message.notification_route_scope in {"admin", "user", "all"}
    )


def routed_notification_messages(
    message: Message, config: NotificationConf, rules: tuple[NotificationRule, ...], *, has_bound_target: bool,
) -> Iterator[Message]:
    """
    生成独立发送副本，确保个人范围始终使用绑定地址。
    """
    matched = tuple(
        rule for rule in rules
        if matches_notification_rule(message, rule, check_time=not notification_rules_are_frozen())
    )
    if config.type in {"webpush", "apppush"}:
        yield from _account_messages(message, config.type, matched)
        return
    planned: dict[tuple[str | None, str | None, str | None], tuple[Message, list[NotificationRule]]] = {}
    key: tuple[str | None, str | None, str | None]
    for rule in matched:
        if rule.fallback_only and has_bound_target:
            continue
        if rule.kind == "bound" and not has_bound_target:
            continue
        if rule.kind != "bound" and message.notification_route_scope == "user":
            continue
        if rule.kind in {"bound", "webhook"}:
            key = (None, None, None)
            routed = message
            if rule.kind == "bound":
                bound = _bound_address(message, config.type or "")
                if bound is not None:
                    addressed = _addressed_message(message, config, bound)
                    key = (str(addressed.userid or ""), addressed.original_chat_id, addressed.thread_id)
        elif rule.kind == "all" and config.type == "synologychat":
            key = ("platform_all", None, None)
            routed = message.model_copy(update={"targets": None})
        else:
            routed = _addressed_message(message, config, rule)
            key = (str(routed.userid or ""), routed.original_chat_id, routed.thread_id)
        if key in planned:
            planned[key][1].append(rule)
        else:
            planned[key] = routed, [rule]
    for routed, selected in planned.values():
        with notification_sending_windows(tuple(selected)):
            yield routed


def _bound_address(message: Message, provider: str) -> NotificationRule | None:
    """
    按适配器的绑定目标优先级合并相同收件人的窗口，群组与私聊保持隔离。
    """
    targets = message.targets or {}
    fields = {
        "telegram": ("telegram_userid",), "wechat": ("wechat_userid",),
        "wechatclawbot": ("wechatclawbot_userid",), "discord": ("discord_userid",),
        "slack": ("slack_userid",), "vocechat": ("vocechat_userid",),
        "synologychat": ("synologychat_userid",),
        "qqbot": ("qq_userid", "qq_openid", "qq_group_openid", "qq_group"),
        "feishu": ("feishu_openid", "feishu_userid", "feishu_chat_id"),
    }
    for field in fields.get(provider, ()):
        target = _text(targets.get(field))
        if not target:
            continue
        kind: NotificationTargetKind = "conversation" if field in {"qq_group_openid", "qq_group", "feishu_chat_id"} else "user"
        # 飞书 user_id 与 open_id 属于不同身份空间，不能因文本相同而合并。
        if field == "feishu_userid":
            return None
        return NotificationRule(kind=kind, target=target, enabled=False, message_types=())
    return None


def _account_messages(
    message: Message, provider: str, rules: tuple[NotificationRule, ...],
) -> Iterator[Message]:
    """
    只在宿主已解析的账号集合内筛选设备所有者，每个账号最多投递一次。
    """
    target = (message.targets or {}).get(provider)
    if isinstance(target, str) and target:
        permitted = (target,)
    elif isinstance(target, tuple) and message.notification_route_scope == "all":
        permitted = target
    else:
        return
    for owner in dict.fromkeys(permitted):
        selected = tuple(rule for rule in rules if rule.kind == "bound" or (rule.kind == "account" and rule.target == owner))
        if selected:
            with notification_sending_windows(selected):
                yield message.model_copy(update={"targets": {provider: owner}})


def _addressed_message(message: Message, config: NotificationConf, rule: NotificationRule) -> Message:
    """
    将已确认的投递目标编码为对应模块的显式地址。
    """
    provider = config.type
    target = rule.target
    if rule.kind == "all":
        target = "@all"
    elif rule.kind == "conversation":
        if provider in {"wechat", "qqbot"}:
            target = f"group:{target}"
        elif provider == "vocechat":
            target = f"GID#{target}"
        elif provider in {"feishu", "discord", "slack"} and not str(target).startswith("#"):
            return message.model_copy(update={
                "userid": target if provider in {"slack", "discord"} else None, "original_chat_id": target, "thread_id": rule.thread_id, "targets": None,
            })
    return message.model_copy(update={
        "userid": target, "original_chat_id": target if provider == "telegram" else None,
        "thread_id": rule.thread_id, "targets": None,
    })
