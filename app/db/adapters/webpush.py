"""
将浏览器订阅应用端口适配为独占 SQLAlchemy 短事务。
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.security.user import UserSnapshot
from app.application.webpush import WebPushConflictError, WebPushRegistration, WebPushUnitOfWork
from app.db.adapters.user import SqlAlchemyUserRepository
from app.db.models.user import User
from app.db.oper.webpush import WebPushSubscriptionOper


class SqlAlchemyWebPushUnitOfWork:
    """
    将账号新鲜度校验与订阅比较交换绑定到同一个独占会话。
    """

    def __init__(self, session: Session) -> None:
        """
        保存会话并构造表级操作器。

        :param session: 当前事务的独占会话
        """
        self._session = session
        self._subscriptions = WebPushSubscriptionOper(session)

    def get_user(self, user_id: int) -> Optional[UserSnapshot]:
        """
        以无值变更更新取得 SQLite 写锁或 PostgreSQL 用户行锁后读取账号。

        :param user_id: 已认证账号主键
        :return: 当前账号快照或空
        """
        self._session.execute(update(User).where(User.id == user_id).values(id=User.id))
        return SqlAlchemyUserRepository(self._session).get_by_id(user_id)

    def get(self, endpoint: str) -> Optional[WebPushRegistration]:
        """
        返回现有端点快照。

        :param endpoint: 浏览器端点地址
        :return: 登记快照或空
        """
        return self._subscriptions.get(endpoint)

    def replace(self, registration: WebPushRegistration, previous: Optional[WebPushRegistration]) -> bool:
        """
        暂存新版本，将并发唯一键冲突转为应用冲突而不覆盖新数据。

        :param registration: 待保存的新快照
        :param previous: 已验证归属的旧快照
        :return: 比较交换成功为真
        :raises WebPushConflictError: 并发新增抢先占用端点
        """
        try:
            return self._subscriptions.replace(registration, previous)
        except IntegrityError as error:
            raise WebPushConflictError("订阅已被更新，请重新登记") from error

    def list_active(self, owners: Optional[tuple[str, ...]]) -> tuple[WebPushRegistration, ...]:
        """
        复制当前有效账号的设备集合。

        :param owners: 明确目标集合或管理全体专用的 None
        :return: 会话外不可变快照
        """
        return self._subscriptions.list_active(owners)

    def delete(self, registration: WebPushRegistration) -> bool:
        """
        按确切归属和版本暂存删除。

        :param registration: 投递或撤销读到的快照
        :return: 删除当前版本时为真
        """
        return self._subscriptions.delete(registration)

    def commit(self) -> None:
        """
        仅按应用服务的显式指令提交当前事务。
        """
        self._session.commit()


@contextmanager
def webpush_scope(session_factory: Callable[[], Session]) -> Iterator[WebPushUnitOfWork]:
    """
    创建独占短事务，异常或未提交退出时由会话关闭回滚。

    :param session_factory: 启动组合根提供的会话工厂
    :yields: 不泄漏 ORM 会话的浏览器推送事务端口
    """
    with session_factory() as session:
        yield SqlAlchemyWebPushUnitOfWork(session)
