"""
渠道成员申请、审批、身份投影和实时权限校验。

成员的业务归属由独立身份保存；已有账号关联只通过已验证的双方凭据完成。
"""

import hashlib
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from json import dumps
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

import app.application.messaging.channel as channel_platform
from app.application.messaging.channel.catalog import interaction_channel
from app.application.notification import get_notification_configs
from app.application.outbox import CHANNEL_AUTHORIZATION_RECEIPT_TOPIC, OutboxIntent
from app.application.security.user import UserSnapshot
from app.domain.admission import AdmissionProof, allowed_context_scopes, matches_auto_download
from app.domain.authorization import (
    AUTHORIZATION_SCOPES,
    CHANNEL_OPERATION_SCOPES,
    CHANNEL_PUBLIC_OPERATIONS,
)
from app.schemas.channel import InteractionPolicy
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel

if TYPE_CHECKING:
    from app.application.database import AsyncDatabaseExecutor


class ChannelAuthorizationConflict(ValueError):
    """
    界面版本或选中成员状态发生变化，整批操作需要重新读取后确认。
    """


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


@dataclass(frozen=True, slots=True)
class RuleTaskGrant:
    """
    由一次已验证的规则命中派生的任务授权，保留发起范围与审批版本。

    :param id: 规则任务授权的不可变标识
    :param member_id: 创建任务时所对应的渠道成员
    :param member_version: 创建任务时的成员更新时间，撤权或审批变更后失效
    :param policy_revision: 命中规则所属的交互策略版本
    :param scopes: 本次规则命中明确允许的操作范围
    """

    id: str
    member_id: str
    member_version: float
    policy_revision: int
    scopes: tuple[str, ...]


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

    def connection_reference_counts(self, connection_id: str) -> tuple[int, int]:
        """
        读取此连接保留的成员和定时任务记录数量。

        :param connection_id: 不可变的连接 ID
        :return: 成员记录、以成员授权为主体的任务记录数量
        """
        ...

    def get(self, member_id: str) -> ChannelMember | None:
        """
        按随机身份读取成员。

        :param member_id: 不可变成员 ID
        :return: 成员快照；不存在时为空
        """
        ...

    def get_rule_task_grant(self, grant_id: str) -> RuleTaskGrant | None:
        """
        读取冻结的任务授权。
        """
        ...

    def stage_rule_task_grant(self, grant: RuleTaskGrant) -> None:
        """
        幂等保存同一成员、规则版本和事件范围的授权。
        """
        ...

    def find(self, notification_id: str, sender_id: str | tuple[str, ...]) -> ChannelMember | None:
        """
        精确查找渠道实例内的发送者。

        :param notification_id: 不可变通知实例 ID
        :param sender_id: 完整平台发送者 ID
        :return: 唯一成员快照；未登记时为空
        """
        ...

    def request(self, notification_id: str, channel: str, sender_id: str, display_name: str, *, actor_ids: tuple[str, ...] = (), bindings: tuple[str, ...] = ()) -> ChannelMember:
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
        self, member_id: str, *, status: str, role: str, scopes: tuple[str, ...], reviewer_id: int,
        expected_updated_at: float | None = None,
    ) -> ChannelMember:
        """
        暂存管理端确认的权限与状态，不更换业务归属。

        :param member_id: 随机成员 ID
        :param status: 合法成员状态
        :param role: 成员或管理员角色
        :param scopes: 经用例校验的能力集合
        :param reviewer_id: 已通过系统管理员认证的操作人主键
        :param expected_updated_at: 批量操作读取的版本；为空时沿用原单条更新合同
        :return: 更新后快照；不存在时抛出 ValueError
        """
        ...

    def delete(self, member_id: str, expected_updated_at: float) -> None:
        """
        按版本暂存删除成员及派生规则任务授权，保留业务账号和历史数据。

        :param member_id: 不可变成员身份
        :param expected_updated_at: 经管理用例核对的授权版本
        :raises ChannelAuthorizationConflict: 成员已经变动或删除
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

    def stage_receipt(self, intent: OutboxIntent, now: datetime) -> None:
        """
        将授权变更的回执写入当前事务。

        :param intent: 与已核对成员和连接绑定的回执意图
        :param now: 持久化意图的 UTC 时间
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


_admission_proof: ContextVar[AdmissionProof | None] = ContextVar("channel_admission_proof", default=None)


@contextmanager
def scoped_admission(proof: AdmissionProof | None) -> Iterator[None]:
    """
    限定本次处理的来源证明，作用域退出后不保留上下文授权。
    """
    token = _admission_proof.set(proof)
    try:
        yield
    finally:
        _admission_proof.reset(token)


def channel_search_auto_download(channel: str, source: str, sender_id: str) -> bool:
    """
    按平台和连接重新读取偏好，选中下载仍需调用独立的下载授权。
    """
    try:
        config = ChannelAuthorizationService.notification(channel, source)
    except PermissionError:
        return False
    try:
        policy, proof, sender = _normalized_admission(config, sender_id)
    except ValueError:
        return False
    return matches_auto_download(policy, proof, sender, time.time())


def _normalized_admission(config: NotificationConf, sender_id: str) -> tuple[InteractionPolicy | None, AdmissionProof | None, str]:
    """
    所有认证入站与历史规则在进入纯领域匹配前解释身份，不增加或延长事件证明。

    :param config: 当前连接及规则版本
    :param sender_id: 发言人原始或规范标识
    :return: 规范策略、保留连接/版本/时效的证明、规范个人标识
    """
    platform = channel_platform.platform_catalog().get(config.type or "")
    if platform is None:
        return config.interaction_policy, None, sender_id
    sender = channel_platform.resolve_actor_ids(platform.channel, sender_id)[0]
    proof = _admission_proof.get()
    if proof is not None:
        proof = replace(
            proof, sender=channel_platform.resolve_actor_ids(platform.channel, proof.sender)[0],
            conversation=platform.conversation(proof.conversation) if proof.conversation is not None else None,
        )
    policy = channel_platform.normalize_policy_actors(platform.provider, config.interaction_policy) if config.interaction_policy else None
    return policy, proof, sender


class ChannelAuthorizationService:
    """
    在通知实例与发送者范围内管理成员，并向执行边界提供实时身份。
    """

    def __init__(self, scope: AuthorizationScope, *, executor: "AsyncDatabaseExecutor | None" = None) -> None:
        """
        保存启动组合根提供的短事务工厂和异步入口使用的数据库执行器。

        :param scope: 每次权限检查独立开启的短事务
        :param executor: 异步调用共享的受管数据库执行器
        """
        self._scope = scope
        self._executor = executor

    @staticmethod
    def notification(channel: str, source: str) -> NotificationConf:
        """
        按渠道类型和来源名称定位当前启用实例，改名不改变成员归属。
        """
        for config in get_notification_configs():
            platform = interaction_channel(config.type)
            if config.interaction_enabled is False:
                continue
            if platform == channel and config.name == source:
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
        sender_id = channel_platform.resolve_actor_ids(channel, sender_id)[0]
        notification = self.notification(channel, source)
        with self._scope() as transaction:
            member = transaction.find(notification.id, channel_platform.resolve_actor_ids(channel, sender_id))
            if member is not None:
                channel_platform.resolve_actor_ids(channel, member.sender_id)
            return member

    def delivery(self, member_id: str) -> tuple[str, str, str] | None:
        """
        按稳定配置身份解析当前私聊投递目标，不把任务结果广播到其它实例。
        """
        with self._scope() as transaction:
            member, _ = self._task_member(transaction, member_id)
            if member is None:
                return None
            config = next((item for item in get_notification_configs() if item.id == member.notification_id), None)
            if config is None or interaction_channel(config.type) != member.channel:
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

    def prepare_task_result(
        self, message: Message, member_id: str, user_id: int | None, user_identity: str | None,
    ) -> Message:
        """
        每次发送任务结果前核对原执行者和定时权限，并限定原连接私聊。

        :param message: 已生成的结果或工具消息
        :param member_id: 任务冻结的渠道授权
        :param user_id: 任务冻结的账号主键
        :param user_identity: 任务冻结的不可变账号身份
        :return: 已绑定投递连接的私聊消息
        :raises PermissionError: 执行账号失效、权限撤销或连接不可用
        """
        user = self.principal(member_id, capability="schedule")
        if not user_identity or user.id != user_id or user.notification_identity != user_identity:
            raise PermissionError("任务执行主体已改变，请从原会话重新创建任务")
        with self._scope() as transaction:
            member, grant = self._task_member(transaction, member_id)
            config = self._receipt_connection(member) if member is not None else None
            if (
                member is None or member.user_identity != user_identity
                or (member.status != "approved" and grant is None)
                or config is None or config.interaction_enabled is False or not config.name
            ):
                raise PermissionError("定时任务的渠道身份或连接已停用")
            if grant is not None:
                self._validate_rule_task_grant(config, member, grant)
            sender = member.sender_id
        outgoing = message.model_copy(update={
            "channel": NotificationChannel(member.channel), "source": config.name,
            "userid": sender, "username": user.name, "targets": None,
            "original_message_id": None, "original_chat_id": None, "thread_id": None,
            "private_delivery": True, "delivery_kind": "task_result",
        })
        outgoing.bind_delivery_connection(config.id, config.identity_namespace or config.id)
        outgoing.bind_task_recipient(member_id, user.id, user_identity)
        return outgoing

    def revalidate_task_result(self, message: Message) -> Message:
        """
        出队派发前从当前授权重建私聊地址，拒绝已经失效的账号或连接绑定。
        """
        recipient = message.task_recipient
        if recipient is None or message.delivery_connection is None:
            raise PermissionError("任务结果缺少有效的执行主体或连接")
        refreshed = self.prepare_task_result(message, *recipient)
        if refreshed.delivery_connection != message.delivery_connection or refreshed.userid != message.userid:
            raise PermissionError("任务结果的连接或接收人已改变")
        return refreshed

    def list_members(self) -> list[ChannelMember]:
        """
        列出管理弹窗所需的成员及待审批申请。
        """
        with self._scope() as transaction:
            return transaction.list_members()

    def connection_reference_counts(self, connection_id: str) -> tuple[int, int]:
        """
        使用只读授权事务取得指定连接的引用数量。

        :param connection_id: 要检查影响的连接 ID
        :return: 成员记录及成员任务的数量
        """
        with self._scope() as transaction:
            return transaction.connection_reference_counts(connection_id)

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
        except (PermissionError, ValueError):
            return False

    def request(self, channel: str, source: str, sender_id: str, display_name: str) -> ChannelMember:
        """
        原子登记申请；重复消息不会增加记录或解除管理员停用。
        """
        sender_id = channel_platform.resolve_actor_ids(channel, sender_id)[0]
        notification = self.notification(channel, source)
        with self._scope() as transaction:
            member = transaction.request(notification.id, channel, sender_id, display_name, actor_ids=channel_platform.resolve_actor_ids(channel, sender_id), bindings=channel_platform.platform_for_channel(channel).binding_fields)
            channel_platform.resolve_actor_ids(channel, member.sender_id)
            transaction.commit()
            return member

    def update(
        self, member_id: str, *, status: str, role: str, scopes: tuple[str, ...], reviewer_id: int,
        queue_receipt: bool = False,
    ) -> ChannelMember:
        """
        校验审批结果并提交，管理端可同时暂存私聊回执。

        :param member_id: 持久化成员 ID
        :param status: 新审批状态
        :param role: 新成员角色
        :param scopes: 新业务权限范围
        :param reviewer_id: 系统管理员 ID
        :param queue_receipt: 是否在同一事务中登记回执
        :return: 当前审批结果
        """
        if status not in {"approved", "rejected", "disabled"} or role not in {"member", "admin"}:
            raise ValueError("授权状态或角色不正确")
        if not set(scopes).issubset(AUTHORIZATION_SCOPES):
            raise ValueError("包含未知的授权能力")
        with self._scope() as transaction:
            current = transaction.get(member_id)
            if current is None:
                raise ValueError("渠道成员不存在")
            if status == "approved":
                sender_id = channel_platform.resolve_actor_ids(current.channel, current.sender_id)[0]
                unique = transaction.find(current.notification_id, channel_platform.resolve_actor_ids(current.channel, sender_id))
                if unique is None or unique.id != member_id:
                    raise ValueError("同一发送者存在冲突授权，请联系管理员核对")
            normalized = tuple(sorted(set(scopes)))
            if queue_receipt and (current.status, current.role, current.scopes) == (status, role, normalized):
                return current
            member = transaction.update(
                member_id, status=status, role=role, scopes=normalized, reviewer_id=reviewer_id
            )
            if queue_receipt:
                self._stage_review_receipt(transaction, member)
            transaction.commit()
            return member

    def manage_members(
        self, action: str, targets: tuple[tuple[str, float], ...], *, reviewer_id: int,
        scopes: tuple[str, ...] = ("search", "subscribe", "download"), confirm_approved: bool = False,
    ) -> tuple[ChannelMember, ...]:
        """
        核对整组选中版本后原子变更，任一冲突时不提交整批操作。

        :param action: approve、reject、disable 或 delete
        :param targets: 被显式选择的成员 ID 和读取版本，最多一百条
        :param reviewer_id: 已认证的网页系统管理员
        :param scopes: 批量通过时明确授予的普通成员能力
        :param confirm_approved: 删除已授权成员的二次确认
        :return: 已提交成员快照；删除时返回删除前快照
        :raises ValueError: 动作、权限或确认不合法
        :raises ChannelAuthorizationConflict: 版本或状态变化，整批未提交
        """
        if action not in {"approve", "reject", "disable", "delete"}:
            raise ValueError("未知的渠道授权操作")
        if not 1 <= len(targets) <= 100 or len({key for key, _ in targets}) != len(targets):
            raise ValueError("请选择一至一百个不重复成员")
        if not set(scopes).issubset(AUTHORIZATION_SCOPES):
            raise ValueError("包含未知的授权能力")
        with self._scope() as transaction:
            selected = []
            for member_id, version in sorted(targets):
                member = transaction.get(member_id)
                if member is None or member.updated_at != version:
                    raise ChannelAuthorizationConflict("成员已更新或删除，请刷新列表后重新选择")
                required = {"approve": "pending", "reject": "pending", "disable": "approved"}.get(action)
                if required is not None and member.status != required:
                    raise ChannelAuthorizationConflict("选中成员状态已变化，请刷新列表后重新选择")
                if action == "delete" and member.status == "approved" and not confirm_approved:
                    raise ValueError("删除已授权成员需要再次确认")
                if action == "approve":
                    sender = channel_platform.resolve_actor_ids(member.channel, member.sender_id)[0]
                    unique = transaction.find(member.notification_id, channel_platform.resolve_actor_ids(member.channel, sender))
                    if unique is None or unique.id != member.id:
                        raise ChannelAuthorizationConflict("同一发送者存在冲突授权，请先逐条核对")
                selected.append(member)
            changed = []
            for member in selected:
                if action == "delete":
                    transaction.delete(member.id, member.updated_at)
                    changed.append(member)
                    continue
                updated = transaction.update(
                    member.id, status={"approve": "approved", "reject": "rejected", "disable": "disabled"}[action],
                    role="member" if action == "approve" else member.role,
                    scopes=tuple(sorted(set(scopes))) if action == "approve" else member.scopes,
                    reviewer_id=reviewer_id, expected_updated_at=member.updated_at,
                )
                self._stage_review_receipt(transaction, updated)
                changed.append(updated)
            transaction.commit()
            return tuple(changed)

    @staticmethod
    def _receipt_connection(member: ChannelMember) -> NotificationConf | None:
        """
        查找当前仍可投递回执的原连接。
        """
        return next((item for item in get_notification_configs()
                     if item.id == member.notification_id and interaction_channel(item.type) == member.channel), None)

    def _stage_review_receipt(self, transaction: AuthorizationUnitOfWork, member: ChannelMember) -> None:
        """
        冻结回执对应的审批版本和连接身份，避免恢复时向新配置发送陈旧结果。
        """
        config = self._receipt_connection(member)
        if config is None:
            return
        now = datetime.now(timezone.utc)
        transaction.stage_receipt(OutboxIntent(
            event_key=f"channel.authorization.receipt:{member.id}:{member.updated_at.hex()}",
            topic=CHANNEL_AUTHORIZATION_RECEIPT_TOPIC,
            payload={
                "member_id": member.id, "version": member.updated_at,
                "connection_id": config.id, "identity_namespace": config.identity_namespace or config.id,
            },
        ), now)

    def receipt_target(
        self, member_id: str, version: float, connection_id: str, identity_namespace: str,
    ) -> tuple[ChannelMember, str] | None:
        """
        仅解析仍有效的审批回执，连接改名时使用当前名称。

        :param member_id: 产生回执的授权记录
        :param version: 审批变动时的 UTC 秒
        :param connection_id: 回执原连接 ID
        :param identity_namespace: 回执原连接身份空间
        :return: 当前成员和连接名称，过期或无法定位时为空
        """
        with self._scope() as transaction:
            member = transaction.get(member_id)
            if member is None or member.updated_at != version or member.notification_id != connection_id:
                return None
            config = self._receipt_connection(member)
            if config is None or (config.identity_namespace or config.id) != identity_namespace or not config.name:
                return None
            channel_platform.resolve_actor_ids(member.channel, member.sender_id)
            return member, config.name

    @staticmethod
    def _task_member(
        transaction: AuthorizationUnitOfWork, authorization_id: str,
    ) -> tuple[ChannelMember | None, RuleTaskGrant | None]:
        """
        任务引用真实成员或规则派生授权，后者不替代成员的审批记录。
        """
        if authorization_id.startswith("g"):
            grant = transaction.get_rule_task_grant(authorization_id)
            return (transaction.get(grant.member_id), grant) if grant else (None, None)
        return transaction.get(authorization_id), None

    @staticmethod
    def _validate_rule_task_grant(config: NotificationConf, member: ChannelMember, grant: RuleTaskGrant) -> None:
        """
        策略变化、人工审查或成员归属变化后，旧规则授权停止执行。
        """
        policy = config.interaction_policy
        if (
            member.status != "pending" or member.updated_at != grant.member_version
            or policy is None or not policy.enabled or policy.admission not in {"manual_rules", "rules"}
            or policy.revision != grant.policy_revision or "schedule" not in grant.scopes
        ):
            raise PermissionError("渠道规则或成员授权已变更，请从符合规则的会话重新创建定时任务")

    def rule_task_principal(self, channel: str, source: str, sender_id: str) -> UserSnapshot | None:
        """
        只为当前可信消息中明确获得定时任务权限的待审批成员建立受限身份。
        """
        proof = _admission_proof.get()
        if proof is None:
            return None
        config = self.notification(channel, source)
        policy = config.interaction_policy
        scopes = self.context_scopes(config, sender_id)
        if policy is None or "schedule" not in scopes:
            return None
        sender_id = channel_platform.resolve_actor_ids(channel, sender_id)[0]
        with self._scope() as transaction:
            member = transaction.request(config.id, channel, sender_id, sender_id, actor_ids=channel_platform.resolve_actor_ids(channel, sender_id), bindings=channel_platform.platform_for_channel(channel).binding_fields)
            if member.status != "pending":
                return None
            # 同规则版本和来源复用任务列表。群、私聊与不同平台发送者的入口互相隔离。
            signature = dumps([
                member.id, member.updated_at, proof.sender, proof.conversation, proof.private,
                policy.revision, sorted(scopes),
            ], ensure_ascii=True)
            grant_id = "g" + hashlib.sha256(signature.encode()).hexdigest()[:31]
            grant = RuleTaskGrant(grant_id, member.id, member.updated_at, policy.revision, tuple(sorted(scopes)))
            transaction.stage_rule_task_grant(grant)
            transaction.commit()
        return self.principal(grant_id, capability="schedule")

    def principal(self, member_id: str, *, capability: str | None = None) -> UserSnapshot:
        """
        每次执行重新检查渠道、成员和关联账号，返回最小有效权限投影。

        :param member_id: 宿主持久化或签名声明中的成员身份
        :param capability: 额外要求的能力，未指定时仅检查可执行状态
        :return: 业务归属账号与当前有效权限
        :raises PermissionError: 渠道被删停、成员失权或关联账号不可用
        """
        with self._scope() as transaction:
            member, grant = self._task_member(transaction, member_id)
            if member is None or (grant is None and member.status != "approved"):
                raise PermissionError("渠道成员尚未授权或已停用，请联系管理员")
            try:
                sender_id = channel_platform.resolve_actor_ids(member.channel, member.sender_id)[0]
                unique = transaction.find(member.notification_id, channel_platform.resolve_actor_ids(member.channel, sender_id))
            except ValueError as error:
                raise PermissionError("历史群身份或冲突授权需要重新核对实际发送者") from error
            if unique is None or unique.id != member.id:
                raise PermissionError("同一发送者存在冲突授权，请联系管理员核对")
            configs = {config.id: config for config in get_notification_configs()}
            config = configs.get(member.notification_id)
            if (
                config is None or config.interaction_enabled is False
                or interaction_channel(config.type) != member.channel
            ):
                raise PermissionError("通知渠道不存在、已停用或类型已改变")
            if grant is not None:
                self._validate_rule_task_grant(config, member, grant)
            user = transaction.user(member.user_id)
            linked = transaction.user(member.linked_user_id) if member.linked_user_id is not None else None
            if (
                user is None
                or not user.is_active
                or user.notification_identity != member.user_identity
                or (member.linked_user_id is not None and (linked is None or not linked.is_active))
            ):
                raise PermissionError("渠道成员关联的账号不存在或已停用")
            admin = grant is None and member.role == "admin" and (linked is None or linked.is_superuser)
            scopes = set(grant.scopes if grant else AUTHORIZATION_SCOPES if admin else member.scopes)
            if linked is not None and not linked.is_superuser:
                scopes = {
                    scope
                    for scope in scopes
                    if linked.permissions.get(
                        "subscribe" if scope == "schedule" else "manage" if scope == "download" else scope
                    )
                    is True
                }
            if grant is not None and "schedule" not in scopes:
                raise PermissionError("关联账号没有定时任务权限")
            if capability and not admin and capability not in scopes:
                raise PermissionError(f"当前渠道成员未获授权使用此能力：{capability}")
            permissions: dict[str, Any] = {scope: scope in scopes for scope in AUTHORIZATION_SCOPES}
            permissions.update(discovery=True, manage=False)
            return replace(
                user, is_superuser=admin, permissions=MappingProxyType(permissions), authorization_id=member_id
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

    async def async_authorize_operation(self, member_id: str, operation_id: str) -> UserSnapshot:
        """
        在受管数据库线程复核当前成员权限，等待短事务完全结束。

        :param member_id: 本次业务请求绑定的渠道授权 ID
        :param operation_id: 本次操作的权限名称
        :return: 按当前权限和账号归属解析的操作主体
        """
        if self._executor is None:
            raise RuntimeError("渠道授权的数据库执行器尚未装配")
        return await self._executor.run(partial(self.authorize_operation, member_id, operation_id))

    def resolve(self, channel: str, source: str, sender_id: str, *, capability: str | None = None) -> UserSnapshot:
        """
        将消息入口身份转换为当前可执行的统一渠道主体。
        """
        member = self.find(channel, source, sender_id)
        if member is None:
            raise PermissionError("此操作需要授权，请发送“申请使用”，由管理员批准后重试；已有账号可先完成账号绑定")
        return self.principal(member.id, capability=capability)

    def context_scopes(self, config: NotificationConf, sender_id: str) -> frozenset[str]:
        """
        使用当前连接策略复核宿主附加的事件证明。
        """
        if config.interaction_enabled is False:
            return frozenset()
        try:
            policy, proof, sender = _normalized_admission(config, sender_id)
        except ValueError:
            return frozenset()
        return allowed_context_scopes(policy, proof, sender, time.time())

    def resolve_operation(
        self, channel: str, source: str, sender_id: str, operation_id: str,
    ) -> UserSnapshot:
        """
        对当前发言人执行与业务 API 共用的操作授权，或采用本次事件的有限授权。
        """
        member = self.find(channel, source, sender_id)
        if member is not None and member.status == "approved":
            return self.authorize_operation(member.id, operation_id)
        config = self.notification(channel, source)
        scopes = self.context_scopes(config, sender_id)
        capability = CHANNEL_OPERATION_SCOPES.get(operation_id)
        if not scopes or capability not in scopes or (member and member.status != "pending"):
            raise PermissionError("此操作需要管理员授权")
        return self._contextual_user(config, channel, sender_id, capability)

    @staticmethod
    def verify_operation_owner(actor: UserSnapshot, owner_identity: str | None) -> None:
        """
        核对当前授权解析出的账号仍是请求发起时的账号。

        :param actor: 使用当前授权重新解析的操作主体
        :param owner_identity: 已绑定到当前业务请求的不可变账号身份
        """
        if not owner_identity or actor.notification_identity != owner_identity:
            raise PermissionError("渠道账号归属已改变，请重新发起操作")

    def _contextual_user(
        self, config: NotificationConf, channel: str, sender_id: str, capability: str,
    ) -> UserSnapshot:
        """
        为已准入的本次操作取得独立业务归属，保留成员原审批状态。
        """
        sender_id = channel_platform.resolve_actor_ids(channel, sender_id)[0]
        with self._scope() as transaction:
            member = transaction.request(config.id, channel, sender_id, sender_id, actor_ids=channel_platform.resolve_actor_ids(channel, sender_id), bindings=channel_platform.platform_for_channel(channel).binding_fields)
            if member.status != "pending":
                raise PermissionError("此操作需要管理员授权")
            user = transaction.user(member.user_id)
            if not user or not user.is_active or user.notification_identity != member.user_identity:
                raise PermissionError("渠道成员关联的账号不存在或已停用")
            if member.linked_user_id is not None:
                permission = {"search": "search", "subscribe": "subscribe", "download": "manage"}[capability]
                linked = transaction.user(member.linked_user_id)
                if not linked or not linked.is_active or not (linked.is_superuser or linked.permissions.get(permission) is True):
                    raise PermissionError("关联账号没有此操作的授权")
            transaction.commit()
            return replace(
                user, is_superuser=False, permissions=MappingProxyType({capability: True}), authorization_id=None,
            )

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
            transaction.observe_aliases(
                notification.id, channel_platform.resolve_actor_ids(channel, sender_id)[0],
                tuple(dict.fromkeys(candidate for value in (sender_id, *aliases) if value for candidate in channel_platform.resolve_actor_ids(channel, value))),
            )
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
