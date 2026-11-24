"""
渠道成员的会话内查询、申请去重和权限暂存。
"""

import time
from typing import cast
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models.agenttask import AgentTask
from app.db.models.authorization import ChannelAuthorization, ChannelBinding, ChannelRuleTaskGrant
from app.db.models.systemconfig import SystemConfig
from app.db.models.user import User
from app.domain.authorization import DEFAULT_MEMBER_SCOPES
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

    def reference_counts(self, connection_id: str) -> tuple[int, int]:
        """
        在同一查询中统计成员与任务，任务按原成员授权 ID 关联。

        :param connection_id: 渠道稳定身份
        :return: 该连接的成员和任务记录数量
        """
        members = select(func.count(ChannelAuthorization.id)).where(
            ChannelAuthorization.notification_id == connection_id,
        ).scalar_subquery()
        connection_members = select(ChannelAuthorization.id).where(ChannelAuthorization.notification_id == connection_id)
        grants = select(ChannelRuleTaskGrant.id).where(ChannelRuleTaskGrant.member_id.in_(connection_members))
        tasks = select(func.count(AgentTask.id)).where(or_(
            AgentTask.execution_authorization_id.in_(connection_members),
            AgentTask.execution_authorization_id.in_(grants),
        )).scalar_subquery()
        member_count, task_count = self._session.execute(select(members, tasks)).one()
        return int(member_count), int(task_count)

    def get(self, member_id: str) -> ChannelAuthorization | None:
        """
        按不可变随机身份查询成员。
        """
        return cast(ChannelAuthorization | None, self._session.get(ChannelAuthorization, member_id))

    def get_rule_task_grant(self, grant_id: str) -> ChannelRuleTaskGrant | None:
        """
        在当前会话查找冻结的规则任务权限。
        """
        return cast(ChannelRuleTaskGrant | None, self._session.get(ChannelRuleTaskGrant, grant_id))

    def stage_rule_task_grant(
        self, grant_id: str, member_id: str, member_version: float, policy_revision: int, scopes: tuple[str, ...],
    ) -> None:
        """
        相同范围的并发请求复用同一个任务授权身份。
        """
        table = ChannelRuleTaskGrant
        insert = postgres_insert if self._session.get_bind().dialect.name == "postgresql" else sqlite_insert
        self._session.execute(insert(table).values(
            id=grant_id, member_id=member_id, member_version=member_version,
            policy_revision=policy_revision, scopes=list(scopes),
        ).on_conflict_do_nothing(index_elements=[table.id]))

    def find(self, notification_id: str, sender_id: str | tuple[str, ...]) -> ChannelAuthorization | None:
        """
        精确匹配实例内发送者及平台证明的别名，不跨实例匹配。
        """
        candidates = (sender_id,) if isinstance(sender_id, str) else sender_id
        rows = self._session.scalars(
            select(ChannelAuthorization).where(ChannelAuthorization.notification_id == notification_id)
        )
        matches = []
        for row in rows:
            if row.sender_id in candidates or set(candidates).intersection(row.aliases or []):
                matches.append(row)
        if len(matches) > 1:
            raise ValueError("同一发送者存在冲突授权，请联系管理员核对")
        return matches[0] if matches else None

    def request(self, notification_id: str, channel: str, sender_id: str, display_name: str, *, actor_ids: tuple[str, ...] = (), bindings: tuple[str, ...] = ()) -> ChannelAuthorization:
        """
        暂存唯一申请及不可登录的内部归属；新关联必须另行验证双方身份。
        """
        # 配置行的数据库锁覆盖查重与新增；多进程并发申请不会留下重复记录或孤立账号。
        self._session.execute(
            update(SystemConfig).where(SystemConfig.key == SystemConfigKey.Notifications.value).values(id=SystemConfig.id)
        )
        existing = self.find(notification_id, actor_ids or (sender_id,))
        if existing is not None:
            return existing
        identity = uuid4().hex
        user = User(
            name=f"channel_{identity}",
            account_type="channel",
            is_active=True,
            is_superuser=False,
            hashed_password=None,
            notification_identity=uuid4().hex,
            settings={bindings[0]: sender_id} if bindings else {},
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
        self, member_id: str, *, status: str, role: str, scopes: tuple[str, ...], reviewer_id: int,
        expected_updated_at: float | None = None,
    ) -> ChannelAuthorization | None:
        """
        暂存状态及能力变化，保留身份和业务归属。

        :param expected_updated_at: 批量操作使用的比较版本，冲突时返回空且不覆盖当前记录
        """
        if expected_updated_at is not None:
            return cast(ChannelAuthorization | None, self._session.scalar(
                update(ChannelAuthorization).where(
                    ChannelAuthorization.id == member_id, ChannelAuthorization.updated_at == expected_updated_at,
                ).values(
                    status=status, role=role, scopes=list(scopes), reviewed_by=reviewer_id,
                    updated_at=max(time.time(), expected_updated_at + 0.000001),
                ).returning(ChannelAuthorization).execution_options(populate_existing=True)
            ))
        member = self.get(member_id)
        if member is None:
            raise ValueError("渠道成员不存在")
        member.status, member.role, member.scopes = status, role, list(scopes)
        member.updated_at = time.time()
        member.reviewed_by = reviewer_id
        self._session.flush()
        return member

    def delete(self, member_id: str, expected_updated_at: float) -> bool:
        """
        删除选中版本及派生授权，不删除业务账号、订阅、下载和任务历史。

        :param member_id: 被显式选择的成员身份
        :param expected_updated_at: 删除前确认的版本
        :return: 当前版本被成功删除时为真
        """
        removed = self._session.scalar(delete(ChannelAuthorization).where(
            ChannelAuthorization.id == member_id, ChannelAuthorization.updated_at == expected_updated_at,
        ).returning(ChannelAuthorization.id))
        if removed is None:
            return False
        self._session.execute(delete(ChannelRuleTaskGrant).where(ChannelRuleTaskGrant.member_id == member_id))
        return True

    def stage_binding(self, code_hash: str, user_id: int, user_identity: str, expires_at: float) -> None:
        """
        暂存唯一关联码摘要，生成新码时撤销该网页账号的旧码。
        """
        self._lock_binding_user(user_id, user_identity)
        self._session.execute(delete(ChannelBinding).where(ChannelBinding.user_id == user_id))
        self._session.add(
            ChannelBinding(id=code_hash, user_id=user_id, user_identity=user_identity, expires_at=expires_at)
        )
        self._session.flush()

    def _lock_binding_user(self, user_id: int, user_identity: str) -> User:
        """
        让生成和消费按相同顺序锁定网页账号，核对不可变身份。
        """
        statement = (
            update(User)
            .where(
                User.id == user_id, User.notification_identity == user_identity,
                User.account_type == "local", User.is_active.is_(True),
            )
            .values(id=User.id).returning(User)
            .execution_options(populate_existing=True)
        )
        user = cast(User | None, self._session.scalar(statement))
        if user is None:
            raise ValueError("关联账号已失效，请重新登录后生成关联码")
        return user

    def _consume_binding_account(self, code_hash: str, now: float) -> User:
        """
        取得目标账号并消费仍有效的码，生成新码若先完成则旧码不再可用。
        """
        active_code = (ChannelBinding.id == code_hash, ChannelBinding.expires_at > now)
        claim = self._session.execute(
            select(ChannelBinding.user_id, ChannelBinding.user_identity).where(*active_code)
        ).first()
        if claim is None:
            raise ValueError("关联码无效、已使用或已过期，请在 MoviePilot 中重新生成")
        user = self._lock_binding_user(claim.user_id, claim.user_identity)
        consumed = self._session.scalar(
            delete(ChannelBinding).where(*active_code, ChannelBinding.user_id == user.id).returning(ChannelBinding.id)
        )
        if consumed is None:
            raise ValueError("关联码无效、已使用或已过期，请在 MoviePilot 中重新生成")
        return user

    def consume_binding(self, member_id: str, code_hash: str, now: float) -> ChannelAuthorization:
        """
        消费验证码并迁移本人订阅和下载归属；运行中的任务与归属冲突拒绝合并。
        """
        from app.db.models.agenttask import AgentTask
        from app.db.models.downloadhistory import DownloadHistory
        from app.db.models.subscribe import Subscribe
        from app.db.models.subscribehistory import SubscribeHistory

        user = self._consume_binding_account(code_hash, now)
        member = cast(
            ChannelAuthorization | None,
            self._session.scalar(
                select(ChannelAuthorization).where(ChannelAuthorization.id == member_id).with_for_update()
            ),
        )
        if member is None:
            raise ValueError("关联账号已失效，请重新登录后生成关联码")
        if member.linked_user_id is not None and member.linked_user_id != user.id:
            raise ValueError("此渠道身份已关联其它账号，请先联系管理员核对")
        rule_grants = select(ChannelRuleTaskGrant.id).where(ChannelRuleTaskGrant.member_id == member.id)
        if self._session.scalar(
            select(AgentTask.id)
            .where(
                or_(AgentTask.execution_authorization_id == member.id, AgentTask.execution_authorization_id.in_(rule_grants)),
                AgentTask.last_status == "running",
            )
            .limit(1)
        ):
            raise ValueError("此成员仍有执行中的任务，请等待任务结束后重新关联")
        previous = self._session.get(User, member.user_id)
        if previous is None or previous.notification_identity != member.user_identity:
            raise ValueError("原业务归属已失效，无法关联")
        if previous.account_type == "channel" and previous.id != user.id:
            for model in (Subscribe, SubscribeHistory, DownloadHistory):
                # 使用 ORM 属性更新，让订阅身份键等写入不变量一并重新计算。
                for record in self._session.scalars(
                    select(model).where(model.owner_identity == member.user_identity).with_for_update()
                ):
                    record.owner_identity = user.notification_identity
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
