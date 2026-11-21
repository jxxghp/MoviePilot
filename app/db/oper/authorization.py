"""
渠道成员的会话内查询、申请去重和权限暂存。
"""

import time
from typing import cast
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db.models.authorization import ChannelAuthorization, ChannelBinding
from app.db.models.systemconfig import SystemConfig
from app.db.models.user import User
from app.domain.authorization import CHANNEL_BINDINGS, DEFAULT_MEMBER_SCOPES
from app.schemas.types import SystemConfigKey


class ChannelAuthorizationOper:
    """
    使用调用方持有的 Session，不提交事务或发送消息。
    """

    def __init__(self, session: Session) -> None:
        """
        保存当前独占事务的同步 Session。
        """
        self._session = session

    def list(self) -> list[ChannelAuthorization]:
        """
        按申请时间逆序返回全部授权记录。
        """
        return list(
            self._session.scalars(select(ChannelAuthorization).order_by(ChannelAuthorization.created_at.desc()))
        )

    def get(self, member_id: str) -> ChannelAuthorization | None:
        """
        按不可变随机身份查询成员。
        """
        return cast(ChannelAuthorization | None, self._session.get(ChannelAuthorization, member_id))

    def find(self, notification_id: str, sender_id: str) -> ChannelAuthorization | None:
        """
        精确匹配实例内发送者及平台证明的别名，不跨实例匹配。
        """
        rows = self._session.scalars(
            select(ChannelAuthorization).where(ChannelAuthorization.notification_id == notification_id)
        )
        matches = [row for row in rows if row.sender_id == sender_id or sender_id in (row.aliases or [])]
        return matches[0] if len(matches) == 1 else None

    def request(self, notification_id: str, channel: str, sender_id: str, display_name: str) -> ChannelAuthorization:
        """
        暂存唯一申请及不可登录的内部归属；新关联必须另行验证双方身份。
        """
        # 配置行的数据库锁覆盖查重与新增；多进程并发申请不会留下重复记录或孤立账号。
        self._session.execute(
            update(SystemConfig).where(SystemConfig.key == SystemConfigKey.Notifications.value).values(id=SystemConfig.id)
        )
        existing = self.find(notification_id, sender_id)
        if existing is not None:
            return existing
        keys = CHANNEL_BINDINGS.get(channel, ())
        identity = uuid4().hex
        user = User(
            name=f"channel_{identity}",
            account_type="channel",
            is_active=True,
            is_superuser=False,
            hashed_password=None,
            notification_identity=uuid4().hex,
            settings={keys[0]: sender_id} if keys else {},
            permissions={},
        )
        self._session.add(user)
        self._session.flush()
        now = time.time()
        member = ChannelAuthorization(
            id=identity,
            notification_id=notification_id,
            channel=channel,
            sender_id=sender_id,
            display_name=display_name or sender_id,
            status="pending",
            role="member",
            scopes=list(DEFAULT_MEMBER_SCOPES),
            user_id=user.id,
            user_identity=user.notification_identity,
            linked_user_id=None,
            aliases=[],
            created_at=now,
            updated_at=now,
            origin="request",
            reviewed_by=None,
        )
        self._session.add(member)
        self._session.flush()
        return member

    def update(
        self, member_id: str, *, status: str, role: str, scopes: tuple[str, ...], reviewer_id: int
    ) -> ChannelAuthorization:
        """
        暂存状态及能力变化，保留身份和业务归属。
        """
        member = self.get(member_id)
        if member is None:
            raise ValueError("渠道成员不存在")
        member.status, member.role, member.scopes = status, role, list(scopes)
        member.updated_at = time.time()
        member.reviewed_by = reviewer_id
        self._session.flush()
        return member

    def stage_binding(self, code_hash: str, user_id: int, user_identity: str, expires_at: float) -> None:
        """
        暂存唯一关联码摘要，生成新码时撤销该网页账号的旧码。
        """
        self._session.execute(delete(ChannelBinding).where(ChannelBinding.user_id == user_id))
        self._session.add(
            ChannelBinding(id=code_hash, user_id=user_id, user_identity=user_identity, expires_at=expires_at)
        )
        self._session.flush()

    def consume_binding(self, member_id: str, code_hash: str, now: float) -> ChannelAuthorization:
        """
        消费验证码并迁移本人订阅和下载归属；运行中的任务与归属冲突拒绝合并。
        """
        from app.db.models.agenttask import AgentTask
        from app.db.models.downloadhistory import DownloadHistory
        from app.db.models.subscribe import Subscribe
        from app.db.models.subscribehistory import SubscribeHistory

        claim = self._session.execute(
            delete(ChannelBinding)
            .where(ChannelBinding.id == code_hash, ChannelBinding.expires_at > now)
            .returning(ChannelBinding.user_id, ChannelBinding.user_identity)
        ).first()
        if claim is None:
            raise ValueError("关联码无效、已使用或已过期，请在 MoviePilot 中重新生成")
        member = self.get(member_id)
        user = self._session.get(User, claim.user_id)
        if (
            member is None
            or user is None
            or not user.is_active
            or user.account_type != "local"
            or user.notification_identity != claim.user_identity
        ):
            raise ValueError("关联账号已失效，请重新登录后生成关联码")
        if member.linked_user_id is not None and member.linked_user_id != user.id:
            raise ValueError("此渠道身份已关联其它账号，请先联系管理员核对")
        if self._session.scalar(
            select(AgentTask.id)
            .where(AgentTask.execution_authorization_id == member.id, AgentTask.last_status == "running")
            .limit(1)
        ):
            raise ValueError("此成员仍有执行中的任务，请等待任务结束后重新关联")
        previous = self._session.get(User, member.user_id)
        if previous is None or previous.notification_identity != member.user_identity:
            raise ValueError("原业务归属已失效，无法关联")
        if previous.account_type == "channel" and previous.id != user.id:
            for model in (Subscribe, SubscribeHistory, DownloadHistory):
                # 使用 ORM 属性更新，让订阅身份键等写入不变量一并重新计算。
                for record in self._session.scalars(select(model).where(model.username == previous.name)):
                    record.username = user.name
            self._session.flush()
            self._session.execute(
                update(AgentTask)
                .where(AgentTask.execution_authorization_id == member.id)
                .values(
                    username=user.name, execution_user_id=user.id, execution_user_identity=user.notification_identity
                )
            )
        member.user_id = user.id
        member.user_identity = user.notification_identity
        member.linked_user_id = user.id
        member.updated_at = now
        self._session.flush()
        return member

    def observe_aliases(self, notification_id: str, sender_id: str, aliases: tuple[str, ...]) -> None:
        """
        在单事务内为唯一历史成员登记平台别名，冲突时拒绝合并权限。
        """
        values = {sender_id, *aliases}
        matches = {member.id: member for value in values if (member := self.find(notification_id, value)) is not None}
        if len(matches) > 1:
            raise ValueError("平台发送者 ID 对应多条授权，请管理员在渠道授权中核对")
        if matches:
            member = next(iter(matches.values()))
            member.aliases = sorted(set(member.aliases or []) | (values - {member.sender_id}))
            self._session.flush()
