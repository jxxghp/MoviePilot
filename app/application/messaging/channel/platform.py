"""
模块平台扩展的应用端口；公共用例只消费规范身份、能力与已验证结果。
"""

from collections.abc import Mapping
from typing import Any, Protocol

from app.domain.authorization import ChannelActor, channel_actor_ids
from app.schemas.channel import (
    ChannelConnection,
    ChannelConnectionInfo,
    ChannelMode,
    ChannelPlatformCapabilities,
    InteractionPolicy,
    NotificationRoute,
    NotificationRule,
)
from app.schemas.common import JsonData
from app.schemas.message import Message, MessageAttachmentRead


class ChannelPlatform(Protocol):
    """
    模块实现的协议边界，不决定成员审批、角色或业务权限。

    :ivar api_version: 宿主清单验证的接口版本，不产生平台注册或成员权限
    :ivar legacy_native_image: 平台对旧冻结通知的原图处理事实
    :ivar module_id: 既有模块清单的宿主实现 ID，用于固定解析 owner
    :ivar display_name: 既有模块清单的展示名称
    :ivar attachment_schemes: 由模块拥有的附件 URI scheme
    :ivar text_confirmation: 是否支持既有确认文本入口
    :ivar cache_reconciliation: 是否有本模块缓存需要生命周期对账
    :ivar provider: 连接平台标识
    :ivar channel: 兼容通知渠道名称
    :ivar interaction: 是否有机器人入口
    :ivar binding_fields: 平台拥有的旧账号绑定字段
    """

    api_version: int
    legacy_native_image: bool
    module_id: str
    display_name: str
    provider: str
    channel: str
    interaction: bool
    attachment_schemes: tuple[str, ...]
    text_confirmation: bool
    cache_reconciliation: bool
    binding_fields: tuple[str, ...]

    def actor(self, value: str) -> ChannelActor:
        """
        解释平台主体并标记个人或群组。

        :param value: 平台事件、管理员输入或历史记录中的原始标识
        :return: 不带授权结论的规范身份事实
        """
        ...

    def capabilities(self, connection: ChannelConnection) -> ChannelPlatformCapabilities:
        """
        从已保存的连接声明当前可验证能力。

        :param connection: 当前连接配置，不得返回其中的凭据
        :return: 仅供能力限制及界面展示使用的事实
        """
        ...

    def mode(self, config: Mapping[str, JsonData]) -> ChannelMode:
        """
        解析模块拥有的连接模式。

        :param config: 旧版或新建连接的配置
        :return: 模块支持的模式标识
        """
        ...

    def editable_fields(self, connection: ChannelConnection) -> tuple[str, ...]:
        """
        声明可保留身份而编辑的配置键。

        :param connection: 当前连接
        :return: 可编辑字段的允许列表
        """
        ...

    def form_values(self, connection: ChannelConnection) -> dict[str, JsonData]:
        """
        筛选平台表单可公开回填的值。

        :param connection: 含私有配置的当前连接
        :return: 不含凭据的表单值
        """
        ...

    def patch_config(self, connection: ChannelConnection, patch: dict[str, JsonData]) -> dict[str, JsonData]:
        """
        校验模块字段约束并保留空白敏感输入的原值。

        :param connection: 当前连接
        :param patch: 管理员提交的候选值
        :return: 校验后的完整私有配置
        """
        ...

    def credential_key(self, connection: ChannelConnection) -> str | None:
        """
        选择能通过可信平台接口核验的轮换凭据。

        :param connection: 当前连接及接入模式
        :return: 可轮换的字段，无法核验时为空
        """
        ...

    def inbound_token_key(self) -> str | None:
        """
        返回模块拥有的回调验证键，未提供时为空。
        """
        ...

    def validate_target(self, connection: ChannelConnection, rule: NotificationRule) -> None:
        """
        执行平台地址及线程格式校验。

        :param connection: 当前连接
        :param rule: 通用用例已核对受众限制的规则
        :raises ValueError: 地址不符合平台协议
        """
        ...

    def legacy_rules(
        self, connection: ChannelConnection, route: NotificationRoute
    ) -> tuple[NotificationRule, ...] | None:
        """
        兼容运行时仍可读取的旧投递字段，历史迁移另用固定快照。

        :param connection: 原连接配置
        :param route: 原事件分类与版本
        :return: 等价规则；不支持规则的平台返回空值
        """
        ...

    def target_fields(self) -> frozenset[str]:
        """
        返回已移交投递用途的旧平台字段。
        """
        ...

    def normalize_interaction(self, value: dict[str, Any]) -> dict[str, Any]:
        """
        将本模块的历史交互字段规范化为平台选项。

        :param value: 尚未进入通用 DTO 的记录
        :return: 可由通用 DTO 读取的记录
        """
        ...

    def runtime_config(self, connection: ChannelConnection, policy: InteractionPolicy | None) -> dict[str, JsonData]:
        """
        把平台选项投影为模块运行配置。

        :param connection: 当前连接
        :param policy: 独立的交互用途，可能不存在
        :return: 仅在宿主内部使用的私有配置
        """
        ...

    def validate_interaction(self, options: dict[str, JsonData]) -> dict[str, JsonData]:
        """
        校验模块专属交互选项。

        :param options: 用户提交的平台选项
        :return: 规范化的平台选项
        :raises ValueError: 模块不支持字段或字段格式无效
        """
        ...

    def private_target(self, value: str) -> str:
        """
        校验个人投递地址的协议约束，不决定接收者是否已授权。

        :param value: 宿主已经明确指定的个人地址
        :return: 平台接受的单个个人地址
        :raises PermissionError: 地址不是本平台允许的个人目标
        """
        ...

    def bound_address(self, message: Message) -> NotificationRule | None:
        """
        按平台优先级解析已绑定地址，仅产生路由事实。

        :param message: 宿主已限制受众的消息
        :return: 可用于合并相同目标的规范地址
        """
        ...

    def address_message(self, message: Message, rule: NotificationRule) -> Message:
        """
        将已选规范目标编码为平台需要的显式地址。

        :param message: 原消息及已冻结的投递意图
        :param rule: 经验证的规范目标
        :return: 不修改原对象的投递副本
        """
        ...

    def conversation(self, address: str) -> str:
        """
        解析已认证事件中的平台会话地址。

        :param address: 模块解析得到的原始会话地址
        :return: 用于规则匹配的规范会话标识
        """
        ...

    def attachment_reference(self, kind: str, reference: str) -> MessageAttachmentRead | None:
        """
        由模块解释附件协议，返回既有下载契约的调用计划。

        :param kind: image、audio 或 file
        :param reference: 认证消息解析出的引用
        :return: 模块支持的读取计划，不支持为空
        """
        ...

    def split_notification(self, parts: list[Message]) -> list[Message]:
        """
        在通用附件组合后执行平台专属分段，宿主随后分配稳定分段序号。

        :param parts: 尚未分配分段序号的消息副本
        :return: 确定顺序的平台分段
        """
        ...

    def probe_identity(self, connection: ChannelConnection) -> str | None:
        """
        使用平台诊断核验当前凭据所属账号，客户端由实现关闭。

        :param connection: 候选连接，仅在受管工作线程中调用
        :return: 平台确认的账号身份，无法确认时为空
        """
        ...

    def reconcile(
        self, previous: tuple[ChannelConnectionInfo, ...], current: tuple[ChannelConnectionInfo, ...]
    ) -> None:
        """
        对账模块自己拥有的本地缓存。

        :param previous: 提交前的本模块连接
        :param current: 提交后的本模块连接
        """
        ...

    def export_interaction(self, policy: InteractionPolicy) -> dict[str, Any]:
        """
        导出模块拥有的公开兼容字段，不改变规范策略。

        :param policy: 已校验的通用策略
        :return: 可公开且无凭据的策略
        """
        ...

    def import_options(self, connection: ChannelConnection, policy: InteractionPolicy) -> InteractionPolicy:
        """
        将模块旧配置投影为交互选项，不改变通用权限。

        :param connection: 含旧配置的连接
        :param policy: 当前交互策略
        :return: 导入平台选项后的策略
        """
        ...

    def manage(self, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """
        执行本模块的已保存连接管理动作。

        :param action: 管理动作
        :param params: 宿主已验证的连接上下文
        :return: 模块管理结果，未接管为空
        """
        ...


class ChannelPlatformCatalog(Protocol):
    """
    现有模块能力注册表的平台扩展投影，不建立另一份发现或激活状态。
    """

    def providers(self) -> tuple[ChannelPlatform, ...]:
        """
        返回已声明的平台扩展，不启动连接。
        """
        ...

    def get(self, provider: str) -> ChannelPlatform | None:
        """
        按连接类型查找平台扩展。

        :param provider: 连接平台标识
        :return: 已声明的实现，未知类型为空
        """
        ...


def platform_catalog() -> ChannelPlatformCatalog:
    """
    消费既有运行配置装配端口提供的模块目录。
    """
    from app.application.configuration import get_channel_platform_catalog

    return get_channel_platform_catalog()


def platform_for_channel(channel: str, *, provider: str | None = None) -> ChannelPlatform:
    """
    解析兼容渠道名称，平台声明只提供身份事实。

    :param provider: 连接已确定的平台协议 owner；同传输类型可以有不同 provider
    :param channel: 入站或历史记录的渠道名称
    :return: 唯一匹配的平台实现
    :raises ValueError: 渠道未注册或存在歧义
    """
    if provider is not None:
        entry = platform_catalog().get(provider)
        if entry is None or entry.channel != channel:
            raise ValueError("连接的平台与传输类型不一致")
        return entry
    # 仅无连接上下文的旧调用使用此兼容路径；歧义拒绝，不选择目录第一项。
    matches = [entry for entry in platform_catalog().providers() if entry.channel == channel]
    if len(matches) != 1:
        raise ValueError("渠道身份协议不存在或不唯一")
    return matches[0]


def resolve_actor_ids(channel: str, value: str, *, provider: str | None = None) -> tuple[str, ...]:
    """
    在用例入口解释平台标识，再由领域层独立拒绝非个人主体。

    :param channel: 平台渠道名称
    :param provider: 所属连接已确定的协议 owner；旧无连接调用有歧义时拒绝
    :param value: 认证报文、管理员录入或历史记录的原始标识
    :return: 已通过领域校验的规范个人标识及别名
    """
    return channel_actor_ids(platform_for_channel(channel, provider=provider).actor(value))


def normalize_policy_actors(provider: str, policy: InteractionPolicy) -> InteractionPolicy:
    """
    在配置与历史读取边界规范化个人和会话规则，不产生授权结论。

    :param provider: 规则所属连接的平台标识
    :param policy: 管理员录入或持久化的交互策略
    :return: 平台已解释、领域可直接匹配的策略
    """
    platform = platform_catalog().get(provider)
    if platform is None:
        return policy

    def normalize(rule: Any) -> Any:
        """
        把个人标识交给领域复核，群组只作为会话条件。
        """
        if not rule.target:
            return rule
        if rule.kind == "user":
            target = channel_actor_ids(platform.actor(rule.target))[0]
        elif rule.kind == "group":
            target = platform.conversation(rule.target)
        else:
            return rule
        return rule.model_copy(update={"target": target})

    return policy.model_copy(
        update={
            "admission_rules": tuple(normalize(rule) for rule in policy.admission_rules),
            "auto_download_rules": tuple(normalize(rule) for rule in policy.auto_download_rules),
        }
    )
