"""
将小组件专用授权端口适配为独占短生命周期 SQLAlchemy 事务。
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.application.security.user import UserSnapshot
from app.application.security.widget import WidgetRegistration, WidgetUnitOfWork
from app.db.adapters.user import SqlAlchemyUserRepository
from app.db.models.user import User
from app.db.oper.widget import WidgetRegistrationOper


class SqlAlchemyWidgetUnitOfWork:
    """
    保存一次授权操作的会话，应用服务显式提交后才发布新凭据。
    """

    def __init__(self, session: Session) -> None:
        """
        保存本作用域独占的同步会话，不自行开启第二个事务。

        :param session: 组合根提供的独占会话
        """
        self._session = session
        self._registrations = WidgetRegistrationOper(session)

    def get_user(self, user_id: int, *, lock: bool = False) -> UserSnapshot | None:
        """
        重新读取用户；写入前通过行更新同时取得 SQLite 写锁或 PostgreSQL 行锁。

        :param user_id: 用户主键
        :param lock: 是否为轮换、名额检查和撤销持有排他锁
        :return: 已复制的当前用户快照，账号不存在时为空
        """
        if lock:
            self._session.execute(update(User).where(User.id == user_id).values(id=User.id))
        return SqlAlchemyUserRepository(self._session).get_by_id(user_id)

    def get_registration(self, registration_id: str) -> WidgetRegistration | None:
        """
        在会话内复制记录，调用方不会持有 ORM 或原始凭据。

        :param registration_id: 规范随机登记 UUID
        :return: 摘要快照；记录不存在时为空
        """
        row = self._registrations.get(registration_id)
        if row is None:
            return None
        return WidgetRegistration(
            registration_id=row.registration_id,
            installation_id=row.installation_id,
            user_id=row.user_id,
            owner=row.owner,
            secret_digest=row.secret_digest,
            expires_at=row.expires_at,
        )

    def stage_registration(self, registration: WidgetRegistration, *, now: float, limit: int) -> bool:
        """
        委托表级 Oper 暂存原子替换，调用前必须先锁当前用户。

        :param registration: 当前账号的新登记摘要
        :param now: 过期清理的 UTC Unix 秒
        :param limit: 应用服务规定的每账号有效安装上限
        :return: 成功暂存返回真，名额不足返回假
        """
        return self._registrations.replace(registration, now=now, limit=limit)

    def stage_delete(self, registration_id: str, owner: str) -> bool:
        """
        仅暂存当前账号指定登记的删除，不影响其它安装或账号。

        :param registration_id: 指定的随机登记标识
        :param owner: 已验证的不可变账号身份
        :return: 确实删除时为真
        """
        return self._registrations.delete(registration_id, owner)

    def commit(self) -> None:
        """
        提交本作用域；数据库失败直接传播，凭据不得提前交给客户端。
        """
        self._session.commit()


@contextmanager
def widget_scope(session_factory: Callable[[], Session]) -> Iterator[WidgetUnitOfWork]:
    """
    进入时创建私有会话，离开时回滚所有未提交操作并释放锁。

    :param session_factory: 启动组合根提供的 Session 工厂
    :yields: 生命周期仅限当前同步调用的事务端口
    """
    with session_factory() as session:
        try:
            yield SqlAlchemyWidgetUnitOfWork(session)
        finally:
            session.rollback()
