"""
渠道成员申请、审批、身份投影和实时权限校验。

成员的业务归属由独立身份保存；已有账号关联只通过已验证的双方凭据完成。
"""

import hashlib
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Protocol

from app.application.notification import get_notification_configs
from app.application.security.user import UserSnapshot
from app.domain.authorization import (
    AUTHORIZATION_SCOPES,
    CHANNEL_OPERATION_SCOPES,
    CHANNEL_PUBLIC_OPERATIONS,
    LEGACY_CHANNELS,
)
from app.schemas.system import NotificationConf


@dataclass(frozen=True, slots=True)
class ChannelMember:
    """
    脱离数据库会话的渠道成员记录。

    :param id: 随机且不可变的成员身份
    :param notification_id: 不可变的通知实例 ID
    :param channel: 平台渠道名称
    :param sender_id: 平台提供的完整发送者 ID
    :param display_name: 仅供展示的昵称，不参与认证
    :param status: pending、approved、rejected 或 disabled
    :param role: member 或 admin
    :param scopes: 普通成员可用的能力
    :param user_id: 固定的内部业务归属账号
    :param user_identity: 账号不可变身份，不允许主键复用后继承授权
    :param linked_user_id: 已验证关联的网页账号；为空时独立使用
    :param created_at: 首次申请的 UTC Unix 秒
    :param updated_at: 最近修改的 UTC Unix 秒
    :param origin: request、legacy 或 manual，表示记录来源
    :param reviewed_by: 最近修改授权的系统管理员主键，未审批或历史迁移为空
    """

    id: str
    notification_id: str
    channel: str
    sender_id: str
    display_name: str
    status: str
    role: str
    scopes: tuple[str, ...]
    user_id: int
    user_identity: str
    linked_user_id: int | None
    created_at: float
    updated_at: float
    origin: str
    reviewed_by: int | None


class AuthorizationUnitOfWork(Protocol):
    """
    单次授权操作的独占事务端口，不向用例暴露 ORM。
    """

    def list_members(self) -> list[ChannelMember]:
        """
        返回全部成员的独立快照。

        :return: 按申请时间排序的成员列表
        """
        ...

    def get(self, member_id: str) -> ChannelMember | None:
        """
        按随机身份读取成员。

        :param member_id: 不可变成员 ID
        :return: 成员快照；不存在时为空
        """
        ...

    def find(self, notification_id: str, sender_id: str) -> ChannelMember | None:
        """
        精确查找渠道实例内的发送者。

        :param notification_id: 不可变通知实例 ID
        :param sender_id: 完整平台发送者 ID
        :return: 唯一成员快照；未登记时为空
        """
        ...

    def request(self, notification_id: str, channel: str, sender_id: str, display_name: str) -> ChannelMember:
        """
        幂等暂存申请，不恢复已被拒绝或停用的成员。

        :param notification_id: 已校验的通知实例 ID
        :param channel: 已校验的渠道名称
        :param sender_id: 完整发送者 ID
        :param display_name: 仅展示的昵称
        :return: 新申请或既有记录
        """
        ...

    def update(
        self, member_id: str, *, status: str, role: str, scopes: tuple[str, ...], reviewer_id: int
    ) -> ChannelMember:
        """
        暂存管理端确认的权限与状态，不更换业务归属。

        :param member_id: 随机成员 ID
        :param status: 合法成员状态
        :param role: 成员或管理员角色
        :param scopes: 经用例校验的能力集合
        :param reviewer_id: 已通过系统管理员认证的操作人主键
        :return: 更新后快照；不存在时抛出 ValueError
        """
        ...

    def user(self, user_id: int) -> UserSnapshot | None:
        """
        查询当前业务账号或关联账号。

        :param user_id: 持久化账号主键
        :return: 当前账号快照；已删除时为空
        """
        ...

    def stage_binding(self, code_hash: str, user: UserSnapshot, expires_at: float) -> None:
        """
        暂存网页账号确认的一次性关联码摘要。

        :param code_hash: 高熵随机码摘要，不保存原文
        :param user: 当前已认证、启用的网页登录账号
        :param expires_at: UTC Unix 秒，到期后不得消费
        """
        ...

    def consume_binding(self, member_id: str, code_hash: str, now: float) -> ChannelMember:
        """
        原子消费关联码，并将成员本人业务归属合并到已验证网页账号。

        :param member_id: 已由渠道发送者证明的成员身份
        :param code_hash: 用户在原渠道提交的关联码摘要
        :param now: 用于到期校验的 UTC Unix 秒
        :return: 已关联成员快照；无效、到期、冲突或执行中任务导致 ValueError
        """
        ...

    def observe_aliases(self, notification_id: str, sender_id: str, aliases: tuple[str, ...]) -> None:
        """
        只记录同一已验证平台报文中的发送者别名，不合并相互冲突的授权记录。

        :param notification_id: 已定位的稳定通知实例
        :param sender_id: 报文中的规范发送者 ID
        :param aliases: 平台同时提供并证明属于同一人的其它 ID
        """
        ...

    def commit(self) -> None:
        """
        提交已暂存的申请或授权；失败时向上传播且不发送成功通知。
        """
        ...


AuthorizationScope = Callable[[], AbstractContextManager[AuthorizationUnitOfWork]]


class ChannelAuthorizationService:
    """
    在通知实例与发送者范围内管理成员，并向执行边界提供实时身份。
    """

    def __init__(self, scope: AuthorizationScope) -> None:
        """
        保存启动组合根提供的短事务工厂。
        """
        self._scope = scope

    @staticmethod
    def notification(channel: str, source: str) -> NotificationConf:
        """
        按渠道类型和来源名称定位当前启用实例，改名不改变成员归属。
        """
        for config in get_notification_configs():
            definition = LEGACY_CHANNELS.get(config.type or "")
            if definition and definition[0] == channel and config.name == source:
                return config
        raise PermissionError("通知渠道不存在或已停用")

    def memory_owner(self, channel: str, source: str, sender_id: str) -> str:
        """
        用稳定通知实例与平台发送者共同隔离记忆，不与网页登录或其它机器人串用。
        """
        notification = self.notification(channel, source)
        return f"channel:{notification.id}:{sender_id}"

    def find(self, channel: str, source: str, sender_id: str) -> ChannelMember | None:
        """
        查询当前启用实例中的稳定发送者，不根据显示名或群 ID 授权。
        """
        notification = self.notification(channel, source)
        with self._scope() as transaction:
            return transaction.find(notification.id, sender_id)

    def delivery(self, member_id: str) -> tuple[str, str, str] | None:
        """
        按稳定配置身份解析当前私聊投递目标，不把任务结果广播到其它实例。
        """
        with self._scope() as transaction:
            member = transaction.get(member_id)
            if member is None:
                return None
            config = next((item for item in get_notification_configs() if item.id == member.notification_id), None)
            if config is None or LEGACY_CHANNELS.get(config.type or "", (None,))[0] != member.channel:
                return None
            return member.channel, config.name or "", member.sender_id

    def linked_username(self, member: ChannelMember) -> str | None:
        """
        返回已验证关联的当前网页账号名，删除或替换后不暴露新账号资料。
        """
        if member.linked_user_id is None:
            return None
        with self._scope() as transaction:
            user = transaction.user(member.linked_user_id)
            return user.name if user and user.notification_identity == member.user_identity else None

    def list_members(self) -> list[ChannelMember]:
        """
        列出管理弹窗所需的成员及待审批申请。
        """
        with self._scope() as transaction:
            return transaction.list_members()

    def is_admin(self, channel: str, source: str, sender_id: str) -> bool:
        """
        判断发送者在指定启用实例中是否为已批准管理员。

        :param channel: 通知渠道显示值
        :param source: 实例名称，不能跨实例继承权限
        :param sender_id: 平台稳定发送者 ID
        :return: 当前实例内具有有效管理员权限时返回 True
        """
        try:
            return self.resolve(channel, source, sender_id).is_superuser
        except PermissionError:
            return False

    def request(self, channel: str, source: str, sender_id: str, display_name: str) -> ChannelMember:
        """
        原子登记申请；重复消息不会增加记录或解除管理员停用。
        """
        if not sender_id:
            raise ValueError("缺少渠道发送者 ID")
        notification = self.notification(channel, source)
        with self._scope() as transaction:
            member = transaction.request(notification.id, channel, sender_id, display_name)
            transaction.commit()
            return member

    def update(
        self, member_id: str, *, status: str, role: str, scopes: tuple[str, ...], reviewer_id: int
    ) -> ChannelMember:
        """
        校验审批结果并提交；仅由已认证的系统管理员端点调用。
        """
        if status not in {"approved", "rejected", "disabled"} or role not in {"member", "admin"}:
            raise ValueError("授权状态或角色不正确")
        if not set(scopes).issubset(AUTHORIZATION_SCOPES):
            raise ValueError("包含未知的授权能力")
        with self._scope() as transaction:
            member = transaction.update(
                member_id, status=status, role=role, scopes=tuple(sorted(set(scopes))), reviewer_id=reviewer_id
            )
            transaction.commit()
            return member

    def principal(self, member_id: str, *, capability: str | None = None) -> UserSnapshot:
        """
        每次执行重新检查渠道、成员和关联账号，返回最小有效权限投影。

        :param member_id: 宿主持久化或签名声明中的成员身份
        :param capability: 额外要求的能力，未指定时仅检查可执行状态
        :return: 业务归属账号与当前有效权限
        :raises PermissionError: 渠道被删停、成员失权或关联账号不可用
        """
        with self._scope() as transaction:
            member = transaction.get(member_id)
            if member is None or member.status != "approved":
                raise PermissionError("渠道成员尚未授权或已停用，请联系管理员")
            configs = {config.id: config for config in get_notification_configs()}
            config = configs.get(member.notification_id)
            if config is None or LEGACY_CHANNELS.get(config.type or "", (None,))[0] != member.channel:
                raise PermissionError("通知渠道不存在、已停用或类型已改变")
            user = transaction.user(member.user_id)
            linked = transaction.user(member.linked_user_id) if member.linked_user_id is not None else None
            if (
                user is None
                or not user.is_active
                or user.notification_identity != member.user_identity
                or (member.linked_user_id is not None and (linked is None or not linked.is_active))
            ):
                raise PermissionError("渠道成员关联的账号不存在或已停用")
            admin = member.role == "admin" and (linked is None or linked.is_superuser)
            scopes = set(AUTHORIZATION_SCOPES if admin else member.scopes)
            if linked is not None and not linked.is_superuser:
                scopes = {
                    scope
                    for scope in scopes
                    if linked.permissions.get(
                        "subscribe" if scope == "schedule" else "manage" if scope == "download" else scope
                    )
                    is True
                }
            if capability and not admin and capability not in scopes:
                raise PermissionError(f"当前渠道成员未获授权使用此能力：{capability}")
            permissions: dict[str, Any] = {scope: scope in scopes for scope in AUTHORIZATION_SCOPES}
            permissions.update(discovery=True, manage=False)
            return replace(
                user, is_superuser=admin, permissions=MappingProxyType(permissions), authorization_id=member.id
            )

    def authorize_operation(self, member_id: str, operation_id: str) -> UserSnapshot:
        """
        为受控 API 重新校验能力，仅管理员可越过普通成员的操作清单。
        """
        user = self.principal(member_id)
        if user.is_superuser or operation_id in CHANNEL_PUBLIC_OPERATIONS:
            return user
        scope = CHANNEL_OPERATION_SCOPES.get(operation_id)
        if scope is None or user.permissions.get(scope) is not True:
            raise PermissionError("当前渠道成员没有此操作的授权，请联系管理员调整权限")
        return user

    def resolve(self, channel: str, source: str, sender_id: str, *, capability: str | None = None) -> UserSnapshot:
        """
        将消息入口身份转换为当前可执行的统一渠道主体。
        """
        member = self.find(channel, source, sender_id)
        if member is None:
            raise PermissionError("此操作需要授权，请发送“申请使用”，由管理员批准后重试；已有账号可先完成账号绑定")
        return self.principal(member.id, capability=capability)

    def create_binding(self, user_id: int) -> tuple[str, float]:
        """
        由已登录用户生成五分钟有效的一次性码，原文不写入数据库或日志。
        """
        code = secrets.token_urlsafe(18)
        expires_at = time.time() + 300
        with self._scope() as transaction:
            user = transaction.user(user_id)
            if user is None or not user.is_active or user.account_type != "local":
                raise PermissionError("请先登录有效的 MoviePilot 网页账号")
            transaction.stage_binding(hashlib.sha256(code.encode()).hexdigest(), user, expires_at)
            transaction.commit()
        return code, expires_at

    def bind(self, channel: str, source: str, sender_id: str, display_name: str, code: str) -> ChannelMember:
        """
        由渠道发送者消费网页登录生成的码，关联不自动批准或解除已有停用。
        """
        member = self.request(channel, source, sender_id, display_name)
        with self._scope() as transaction:
            member = transaction.consume_binding(member.id, hashlib.sha256(code.encode()).hexdigest(), time.time())
            transaction.commit()
            return member

    def observe_aliases(self, channel: str, source: str, sender_id: str, *aliases: str | None) -> None:
        """
        由可信平台适配器补齐历史 ID 别名，前端和模型参数不能调用此身份证明入口。
        """
        notification = self.notification(channel, source)
        with self._scope() as transaction:
            transaction.observe_aliases(notification.id, sender_id, tuple(value for value in aliases if value))
            transaction.commit()


_configured_service: ChannelAuthorizationService | None = None


def configure_channel_authorization(service: ChannelAuthorizationService | None) -> None:
    """
    由启动组合根登记服务；传空值在关闭或测试退出时撤销。
    """
    global _configured_service
    _configured_service = service


def get_channel_authorization() -> ChannelAuthorizationService | None:
    """
    读取当前生命周期的服务，未装配时返回空值并由调用边界拒绝新能力。
    """
    return _configured_service
