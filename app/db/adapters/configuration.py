"""用户配置快照的显式短会话与事务适配器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional, Union

from sqlalchemy.orm import Session

from app.db.oper.userconfig import UserConfigOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.schemas.common import JsonData
from app.schemas.types import UserConfigKey


class TransactionalUserConfigurationRepository:
    """在短事务提交后发布用户配置缓存，并在发布失败时重载事实源。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        snapshot: Optional[UserConfigOper] = None,
    ) -> None:
        """保存会话工厂及进程级用户配置快照。"""
        self._session_factory = session_factory
        self._snapshot = snapshot or UserConfigOper()

    def load_snapshot(self) -> None:
        """使用独立只读会话从数据库发布完整配置快照。"""
        with self._session_factory() as session:
            self._snapshot.load_snapshot(session)

    def get(
        self,
        username: str,
        key: Union[str, UserConfigKey],
    ) -> JsonData:
        """从进程级快照读取一项深拷贝配置。"""
        return self._snapshot.get(username=username, key=key)

    def set(
        self,
        username: str,
        key: Union[str, UserConfigKey],
        value: JsonData,
    ) -> None:
        """提交一项配置后发布快照，发布异常时从数据库恢复快照。"""
        with self._snapshot.write_scope():
            with self._session_factory() as session:
                unit_of_work = SqlAlchemyUnitOfWork(session)
                try:
                    deleted = self._snapshot.stage_set(
                        session,
                        username,
                        key,
                        value,
                    )
                    unit_of_work.commit()
                except Exception:
                    unit_of_work.rollback()
                    raise
            try:
                self._snapshot.publish(
                    username,
                    key,
                    value,
                    deleted=deleted,
                )
            except Exception:
                self.load_snapshot()
                raise

    def publish_rename(self, previous_name: str, current_name: str) -> None:
        """发布已提交用户改名，并在同一写锁内以数据库事实源收口。"""
        with self._snapshot.write_scope():
            try:
                self._snapshot.publish_rename(previous_name, current_name)
            except Exception:
                self.load_snapshot()
                raise
            self.load_snapshot()

    def publish_delete(self, username: str) -> None:
        """发布已提交用户删除，并在同一写锁内以数据库事实源收口。"""
        with self._snapshot.write_scope():
            try:
                self._snapshot.publish_delete(username)
            except Exception:
                self.load_snapshot()
                raise
            self.load_snapshot()
