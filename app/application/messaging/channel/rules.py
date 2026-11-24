"""
内置渠道投递目的地、旧配置迁移及主动事件匹配。
"""

from collections.abc import Iterator

import app.application.messaging.channel as channel_platform
from app.application.messaging.delivery import (
    notification_rule_time_allows,
    notification_rules_are_frozen,
    notification_sending_windows,
)
from app.schemas.channel import (
    ChannelConnection,
    ChannelSettings,
    NotificationRule,
    NotificationTargetKind,
)
from app.schemas.message import Message
from app.schemas.system import NotificationConf


def notification_target_kinds(connection: ChannelConnection) -> tuple[NotificationTargetKind, ...]:
    """
    返回模块为当前连接声明的投递目标类型。
    """
    platform = channel_platform.platform_catalog().get(connection.provider)
    return platform.capabilities(connection).target_kinds if platform else ()


def notification_rule_types(rules: tuple[NotificationRule, ...]) -> tuple[str, ...]:
    """
    为兼容旧查询按原顺序投影规则中使用的事件类型。
    """
    return tuple(dict.fromkeys(kind for rule in rules for kind in rule.message_types))


def notification_target_fields(provider: str) -> frozenset[str]:
    """
    返回应从平台连接配置移交给投递规则的字段。
    """
    platform = channel_platform.platform_catalog().get(provider)
    return platform.target_fields() if platform else frozenset()


def validate_notification_rule(connection: ChannelConnection, rule: NotificationRule) -> None:
    """
    公共用例检查受众能力和候补约束，地址协议由模块校验。
    """
    if rule.kind == "review":
        return
    platform = channel_platform.platform_catalog().get(connection.provider)
    if platform is None or rule.kind not in platform.capabilities(connection).target_kinds:
        raise ValueError("该渠道不支持所选投递目标")
    capabilities = platform.capabilities(connection)
    if rule.owner_identity and not capabilities.account_targets:
        raise ValueError("该渠道不支持按关联账号筛选事件")
    if rule.legacy_usernames and not capabilities.legacy_usernames:
        raise ValueError("该渠道不支持旧用户名条件")
    if rule.fallback_only and rule.kind in {"bound", "account", "webhook"}:
        raise ValueError("仅明确地址或平台全体目标可作为候补")
    if rule.thread_id and not capabilities.thread_id:
        raise ValueError("该渠道不支持话题或线程 ID")
    platform.validate_target(connection, rule)


def migrate_notification_rules(settings: ChannelSettings) -> ChannelSettings:
    """
    运行时兼容读取旧投递字段，正式数据库升级使用固定离线快照。
    """
    for key, route in settings.notifications.items():
        connection = settings.connections[key]
        platform = channel_platform.platform_catalog().get(connection.provider)
        if route.rules is not None or platform is None:
            continue
        rules = platform.legacy_rules(connection, route)
        if rules is None:
            continue
        settings.notifications[key] = route.model_copy(update={"rules": rules})
        if key in settings.interactions:
            settings.interactions[key] = platform.import_options(connection, settings.interactions[key])
        provider_config = {field: value for field, value in connection.provider_config.items() if field not in platform.target_fields()}
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
    platform = channel_platform.platform_catalog().get(config.type or "")
    if platform is not None and platform.capabilities(ChannelConnection(
        id=config.id, name=config.name or config.id, provider=config.type or "",
        identity_namespace=config.identity_namespace or config.id, enabled=bool(config.enabled), revision=1,
        provider_config=dict(config.config or {}), mode=platform.mode(dict(config.config or {})),
    )).account_targets:
        yield from _account_messages(message, platform.provider, matched)
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
    platform = channel_platform.platform_catalog().get(provider)
    return platform.bound_address(message) if platform else None


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
    platform = channel_platform.platform_catalog().get(config.type or "")
    if platform is None:
        raise ValueError("该渠道没有声明地址编码能力")
    return platform.address_message(message, rule)
