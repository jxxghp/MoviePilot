"""
平台扩展的无状态模板；只执行模块声明的数据约束，不包含平台名称或协议分支。
"""

import re
from collections.abc import Mapping
from typing import Any

from app.domain.authorization import ChannelActor
from app.schemas.channel import (
    ChannelConnection,
    ChannelConnectionInfo,
    ChannelMode,
    ChannelPlatformCapabilities,
    InteractionPolicy,
    NotificationRoute,
    NotificationRule,
    NotificationTargetKind,
)
from app.schemas.common import JsonData
from app.schemas.message import Message, MessageAttachmentRead


class ChannelPlatformBase:
    """
    为模块提供字段允许列表和规范路由的通用模板，不授予任何权限。

    :ivar provider: 既有服务配置的平台标识
    :ivar channel: 兼容通知渠道名称
    :ivar interaction: 是否提供认证交互入口
    :ivar cache_reconciliation: 是否拥有需对账的模块缓存
    :ivar text_confirmation: 是否支持已存在的 Agent 确认文本入口
    :ivar attachment_schemes: 本模块解释的显式附件 URI scheme
    :ivar binding_fields: 历史业务账号绑定键，数据本身不授予权限
    :ivar capabilities_model: 无凭据的平台能力描述
    :ivar edit_fields: 可保留平台身份而编辑的字段
    :ivar write_only: 禁止公开回填的敏感字段
    :ivar public_fields: 可公开回填的平台标识
    :ivar default_targets: 旧默认地址及接收对象类型
    :ivar bound_targets: 绑定地址优先级及是否可安全合并
    :ivar obsolete_fields: 已移交用途配置的旧字段
    :ivar target_patterns: 模块声明的接收地址格式
    :ivar target_error: 地址无效时的安全提示
    :ivar topic_key: 旧话题配置键
    :ivar topic_pattern: 平台话题或线程格式
    :ivar username_key: 需兼容的旧用户名过滤键
    :ivar token_key: 专用回调验证令牌键
    :ivar rotation_key: 可经平台诊断核验的轮换凭据键
    :ivar conversation_prefix: 显式会话地址的模块编码前缀
    :ivar conversation_as_chat: 会话写入独立聊天地址
    :ivar conversation_as_user: 会话同时兼容原显式收件人字段
    :ivar user_as_chat: 个人地址也写入平台聊天字段
    :ivar all_address: 平台明确的全体广播地址
    :ivar literal_address_prefixes: 不再转换为聊天字段的原文地址前缀
    :ivar needs_target_review: 无法确定旧默认目标时保留停用待确认规则
    """

    provider: str = ""
    channel: str = ""
    interaction: bool = False
    cache_reconciliation: bool = False
    text_confirmation: bool = False
    attachment_schemes: tuple[str, ...] = ()
    binding_fields: tuple[str, ...] = ()
    capabilities_model: ChannelPlatformCapabilities = ChannelPlatformCapabilities()
    edit_fields: tuple[str, ...] = ()
    write_only: frozenset[str] = frozenset()
    public_fields: frozenset[str] = frozenset()
    default_targets: tuple[tuple[str, NotificationTargetKind], ...] = ()
    bound_targets: tuple[tuple[str, NotificationTargetKind, bool], ...] = ()
    obsolete_fields: frozenset[str] = frozenset()
    target_patterns: Mapping[str, str] = {}
    target_error: str = "请输入有效的平台 ID"
    topic_key: str | None = None
    topic_pattern: str | None = None
    username_key: str | None = None
    token_key: str | None = None
    rotation_key: str | None = None
    conversation_prefix: str = ""
    conversation_as_chat: bool = False
    conversation_as_user: bool = True
    user_as_chat: bool = False
    all_address: str | None = None
    literal_address_prefixes: tuple[str, ...] = ()
    needs_target_review: bool = False

    def actor(self, value: str) -> ChannelActor:
        """
        透传模块已经确认的个人标识；需要协议解释的模块必须覆盖此方法。
        """
        return ChannelActor(value.strip())

    def capabilities(self, connection: ChannelConnection) -> ChannelPlatformCapabilities:
        """
        返回模块的固定能力，依赖连接状态的模块另行覆盖。
        """
        return self.capabilities_model

    def mode(self, config: Mapping[str, JsonData]) -> ChannelMode:
        """
        无专属接入模式的模块使用标准模式。
        """
        return "standard"

    def editable_fields(self, connection: ChannelConnection) -> tuple[str, ...]:
        """
        排除已移交用途配置的字段，保留模块允许编辑的连接字段。
        """
        return tuple(key for key in self.edit_fields if key not in self.target_fields())

    def form_values(self, connection: ChannelConnection) -> dict[str, JsonData]:
        """
        仅回填声明的公开字段及非凭据编辑字段。
        """
        visible = (set(self.editable_fields(connection)) - self.write_only) | self.public_fields
        return {key: value for key, value in connection.provider_config.items() if key in visible}

    def patch_config(self, connection: ChannelConnection, patch: dict[str, JsonData]) -> dict[str, JsonData]:
        """
        拒绝受保护字段；空白凭据保留原值。
        """
        if patch.keys() - set(self.editable_fields(connection)):
            raise ValueError("连接平台身份及受保护字段需通过凭据轮换或新建连接修改")
        return connection.provider_config | {
            key: value for key, value in patch.items() if key not in self.write_only or value not in (None, "")
        }

    def credential_key(self, connection: ChannelConnection) -> str | None:
        """
        默认只返回模块明确声明能核验账号的轮换字段。
        """
        return self.rotation_key

    def inbound_token_key(self) -> str | None:
        """
        返回模块声明的专用回调认证字段。
        """
        return self.token_key

    def target_fields(self) -> frozenset[str]:
        """
        汇总模块已移交投递用途的旧配置字段。
        """
        return (
            frozenset(key for key, _ in self.default_targets)
            | self.obsolete_fields
            | frozenset(key for key in (self.topic_key, self.username_key) if key)
        )

    def validate_target(self, connection: ChannelConnection, rule: NotificationRule) -> None:
        """
        按模块声明的地址与线程格式执行校验。
        """
        pattern = self.target_patterns.get(rule.kind)
        if rule.target is not None and pattern and re.fullmatch(pattern, rule.target) is None:
            raise ValueError(self.target_error)
        if rule.thread_id and (not self.topic_pattern or re.fullmatch(self.topic_pattern, rule.thread_id) is None):
            raise ValueError("请输入有效的平台话题或线程 ID")

    def normalize_interaction(self, value: dict[str, Any]) -> dict[str, Any]:
        """
        无历史专属字段时保留通用交互记录。
        """
        return {key: item for key, item in value.items() if item is not None or key in InteractionPolicy.model_fields}

    def export_interaction(self, policy: InteractionPolicy) -> dict[str, Any]:
        """
        导出通用交互设置，兼容字段由所属平台覆盖。
        """
        return policy.model_dump()

    def validate_interaction(self, options: dict[str, JsonData]) -> dict[str, JsonData]:
        """
        未声明平台表单的模块拒绝额外选项。
        """
        if options:
            raise ValueError("该渠道不支持所提交的平台交互选项")
        return {}

    def runtime_config(self, connection: ChannelConnection, policy: InteractionPolicy | None) -> dict[str, JsonData]:
        """
        无专属投影时复制私有连接配置，调用方不能修改原对象。
        """
        return dict(connection.provider_config)

    def import_options(self, connection: ChannelConnection, policy: InteractionPolicy) -> InteractionPolicy:
        """
        默认没有从连接移交的额外交互选项。
        """
        return policy

    def legacy_addresses(
        self, connection: ChannelConnection
    ) -> tuple[tuple[NotificationTargetKind, str, str | None], ...]:
        """
        按模块声明的优先级读取历史目标，不推断其授权角色。
        """
        config = connection.provider_config
        topic = str(config.get(self.topic_key) or "") if self.topic_key else ""
        if topic.isascii() and topic.isdecimal() and len(topic) < 16 and int(topic) > 0:
            topic = str(int(topic))
        else:
            topic = ""
        return tuple(
            (kind, str(config[key]).strip(), topic or None)
            for key, kind in self.default_targets
            if type(config.get(key)) in (str, int) and config[key]
        )

    def legacy_rules(
        self, connection: ChannelConnection, route: NotificationRoute
    ) -> tuple[NotificationRule, ...] | None:
        """
        把模块的旧默认地址转换为保守规则，无法确认的目标留待管理员处理。
        """
        caps = self.capabilities(connection)
        if not caps.target_kinds:
            return None
        types = route.message_types
        addresses = self.legacy_addresses(connection)
        raw_names = connection.provider_config.get(self.username_key) if self.username_key else None
        if not types and not addresses and not raw_names:
            return ()
        if "webhook" in caps.target_kinds:
            return (NotificationRule(kind="webhook", message_types=types),)
        names = tuple(
            dict.fromkeys(
                name
                for name in (raw_names.split(",") if isinstance(raw_names, str) else ())
                if name and name == name.strip() and len(name) <= 160
            )
        )[:100]
        rules = [
            NotificationRule(
                kind="bound",
                message_types=types,
                legacy_usernames=names,
                enabled=bool(types) and (not raw_names or bool(names)),
            )
        ]
        for index, (kind, target, thread_id) in enumerate(addresses):
            try:
                rule = NotificationRule(
                    kind=kind,
                    target=target,
                    thread_id=thread_id,
                    message_types=types,
                    fallback_only=True,
                    enabled=bool(types) and index == 0,
                )
                self.validate_target(connection, rule)
            except ValueError:
                rule = NotificationRule(kind="review", message_types=types, enabled=False)
            rules.append(rule)
        if not addresses:
            if "all" in caps.target_kinds and types:
                rules.append(NotificationRule(kind="all", message_types=types, audience="all", fallback_only=True))
            elif self.needs_target_review or raw_names and not names:
                rules.append(NotificationRule(kind="review", message_types=types, enabled=False))
        return tuple(rules)

    def bound_address(self, message: Message) -> NotificationRule | None:
        """
        仅以模块能证明的同类地址合并窗口，不合并不同身份空间。
        """
        targets = message.targets or {}
        for field, kind, comparable in self.bound_targets:
            value = targets.get(field)
            if type(value) not in (str, int) or not str(value).strip():
                continue
            return (
                NotificationRule(kind=kind, target=str(value).strip(), enabled=False, message_types=())
                if comparable
                else None
            )
        return None

    def address_message(self, message: Message, rule: NotificationRule) -> Message:
        """
        按模块声明编码地址，保留原消息及其已冻结投递意图。
        """
        target = rule.target
        chat = None
        if rule.kind == "all":
            target = self.all_address
        elif rule.kind == "conversation":
            if self.conversation_as_chat and not any(
                str(target).startswith(prefix) for prefix in self.literal_address_prefixes
            ):
                chat = target
                target = target if self.conversation_as_user else None
            else:
                target = self.conversation_prefix + str(target)
        if self.user_as_chat:
            chat = target
        return message.model_copy(
            update={"userid": target, "original_chat_id": chat, "thread_id": rule.thread_id, "targets": None}
        )

    def conversation(self, address: str) -> str:
        """
        不带专属编码的模块直接使用会话标识。
        """
        return address

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        模板不解释任何平台协议，具体模块按需实现附件引用。
        """
        return None

    def split_notification(self, parts: list[Message]) -> list[Message]:
        """
        没有专属分段要求时保留组合顺序。
        """
        return parts

    def probe_identity(self, connection: ChannelConnection) -> str | None:
        """
        未声明诊断能力的模块不查询外部平台。
        """
        return None

    def reconcile(
        self, previous: tuple[ChannelConnectionInfo, ...], current: tuple[ChannelConnectionInfo, ...]
    ) -> None:
        """
        没有本地平台缓存的模块无需对账。
        """
        return None

    def manage(self, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """
        默认不接管模块既有管理入口。
        """
        return None
