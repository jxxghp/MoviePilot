"""
统一渠道授权端口的短事务与不可变快照适配。
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.outbox import OutboxIntent
from app.application.security.authorization import (
    AuthorizationUnitOfWork,
    ChannelAuthorizationConflict,
    ChannelMember,
    RuleTaskGrant,
)
from app.application.security.user import UserSnapshot
from app.db.adapters.outbox import SqlAlchemyOutboxStager
from app.db.adapters.user import SqlAlchemyUserRepository
from app.db.models.authorization import ChannelAuthorization, ChannelInboundReceipt
from app.db.oper.authorization import ChannelAuthorizationOper


def claim_inbound_event(
    session_factory: Callable[[], Session], fingerprint: str, now: float, expires_at: float,
) -> bool:
    """
    以一个短事务认领事件。仅清理有限批次的过期记录，兼容 SQLite 和 PostgreSQL。
    """
    with session_factory() as session, session.begin():
        row = ChannelInboundReceipt
        insert = postgres_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        statement = insert(row).values(id=fingerprint, expires_at=expires_at)
        claimed = session.scalar(
            statement.on_conflict_do_update(
                index_elements=[row.id], set_={"expires_at": statement.excluded.expires_at},
                where=row.expires_at <= now,
            ).returning(row.id)
        )
        # 跳过已被其它认领事务分批清理的行，过期记录最多每次清理 128 条。
        expired = select(row.id).where(row.expires_at <= now).order_by(row.expires_at, row.id).limit(128)
        if session.get_bind().dialect.name == "postgresql":
            expired = expired.with_for_update(skip_locked=True)
        session.execute(delete(row).where(row.id.in_(expired), row.expires_at <= now))
        return claimed is not None


def _snapshot(row: ChannelAuthorization) -> ChannelMember:
    """
    在 Session 关闭前复制成员身份、状态和能力。
    """
    return ChannelMember(
        id=row.id,
        notification_id=row.notification_id,
        channel=row.channel,
        sender_id=row.sender_id,
        display_name=row.display_name,
        status=row.status,
        role=row.role,
        scopes=tuple(row.scopes),
        user_id=row.user_id,
        user_identity=row.user_identity,
        linked_user_id=row.linked_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        origin=row.origin,
        reviewed_by=row.reviewed_by,
    )


class SqlAlchemyAuthorizationUnitOfWork:
    """
    为一个同步授权用例持有私有 Session，事务由应用服务显式提交。
    """

    def __init__(self, session: Session) -> None:
        """
        绑定本作用域 Session 与表级 Oper。
        """
        self._session = session
        self._members = ChannelAuthorizationOper(session)

    def list_members(self) -> list[ChannelMember]:
        """
        返回按申请时间排序的脱离会话快照。
        """
        return [_snapshot(row) for row in self._members.list()]

    def connection_reference_counts(self, connection_id: str) -> tuple[int, int]:
        """
        在当前会话读取成员与任务引用数量。
        """
        return self._members.reference_counts(connection_id)

    def get(self, member_id: str) -> ChannelMember | None:
        """
        返回指定成员快照，不保留 ORM 引用。
        """
        row = self._members.get(member_id)
        return _snapshot(row) if row else None

    def get_rule_task_grant(self, grant_id: str) -> RuleTaskGrant | None:
        """
        按任务授权身份读取原规则权限。
        """
        row = self._members.get_rule_task_grant(grant_id)
        if row is None:
            return None
        return RuleTaskGrant(row.id, row.member_id, row.member_version, row.policy_revision, tuple(row.scopes))

    def stage_rule_task_grant(self, grant: RuleTaskGrant) -> None:
        """
        在当前事务中暂存推导出的任务授权。
        """
        self._members.stage_rule_task_grant(
            grant.id, grant.member_id, grant.member_version, grant.policy_revision, grant.scopes,
        )

    def find(self, notification_id: str, sender_id: str) -> ChannelMember | None:
        """
        读取通知实例内唯一匹配的发送者。
        """
        row = self._members.find(notification_id, sender_id)
        return _snapshot(row) if row else None

    def request(self, notification_id: str, channel: str, sender_id: str, display_name: str) -> ChannelMember:
        """
        暂存申请并复制数据库生成的内部归属主键。
        """
        return _snapshot(self._members.request(notification_id, channel, sender_id, display_name))

    def update(
        self, member_id: str, *, status: str, role: str, scopes: tuple[str, ...], reviewer_id: int,
        expected_updated_at: float | None = None,
    ) -> ChannelMember:
        """
        暂存权限变动后返回独立快照。
        """
        row = self._members.update(
            member_id, status=status, role=role, scopes=scopes, reviewer_id=reviewer_id,
            expected_updated_at=expected_updated_at,
        )
        if row is None:
            raise ChannelAuthorizationConflict("成员已更新或删除，请刷新列表后重新选择")
        return _snapshot(row)

    def delete(self, member_id: str, expected_updated_at: float) -> None:
        """
        暂存版本匹配的成员删除，冲突交给应用事务整体回滚。
        """
        if not self._members.delete(member_id, expected_updated_at):
            raise ChannelAuthorizationConflict("成员已更新或删除，请刷新列表后重新选择")

    def user(self, user_id: int) -> UserSnapshot | None:
        """
        读取业务归属或已绑定网页账号的当前状态。
        """
        return SqlAlchemyUserRepository(self._session).get_by_id(user_id)

    def stage_binding(self, code_hash: str, user: UserSnapshot, expires_at: float) -> None:
        """
        仅把随机码摘要、已认证账号和到期时间写入当前事务。
        """
        self._members.stage_binding(code_hash, user.id, user.notification_identity, expires_at)

    def stage_receipt(self, intent: OutboxIntent, now: datetime) -> None:
        """
        与授权行一起暂存回执，复用 Outbox 已有的持久化边界。
        """
        SqlAlchemyOutboxStager(self._session).stage(intent, now)

    def consume_binding(self, member_id: str, code_hash: str, now: float) -> ChannelMember:
        """
        原子消费关联码并返回合并归属后的成员快照。
        """
        try:
            return _snapshot(self._members.consume_binding(member_id, code_hash, now))
        except IntegrityError as error:
            raise ValueError("账号关联存在业务归属冲突，原数据未修改，请联系管理员核对后重试") from error

    def observe_aliases(self, notification_id: str, sender_id: str, aliases: tuple[str, ...]) -> None:
        """
        将平台证明的同人标识交给表级 Oper 暂存，不对业务账号执行合并。
        """
        self._members.observe_aliases(notification_id, sender_id, aliases)

    def commit(self) -> None:
        """
        提交完整授权事务；失败时由作用域退出回滚。
        """
        self._session.commit()


@contextmanager
def authorization_scope(session_factory: Callable[[], Session]) -> Iterator[AuthorizationUnitOfWork]:
    """
    为单次授权创建私有 Session，离开时回滚所有未提交操作并释放资源。

    :param session_factory: 启动组合根提供的独立 Session 工厂
    :yields: 当前用例可查询、暂存并提交的授权事务端口
    """
    with session_factory() as session:
        try:
            yield SqlAlchemyAuthorizationUnitOfWork(session)
        finally:
            session.rollback()
