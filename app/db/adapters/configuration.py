"""用户配置快照的显式短会话与事务适配器。"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Optional, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models.systemconfig import SystemConfig
from app.db.oper.userconfig import UserConfigOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.schemas.common import JsonData
from app.schemas.types import SystemConfigKey, UserConfigKey


class SessionSystemConfigurationRepository:
    """复用调用方 Session 锁定和暂存 SystemConfig，且不拥有提交。"""

    def __init__(self, session: Session | AsyncSession) -> None:
        """绑定规则组命令持有的同步或异步 Session。"""
        self._session = session

    def _sync_session(self) -> Session:
        """返回同步 Session，并拒绝同步异步混用。"""
        if not isinstance(self._session, Session):
            raise RuntimeError("该系统配置操作需要同步 Session")
        return self._session

    def _async_session(self) -> AsyncSession:
        """返回异步 Session，并拒绝同步异步混用。"""
        if not isinstance(self._session, AsyncSession):
            raise RuntimeError("该系统配置操作需要异步 Session")
        return self._session

    def get_for_update(self, key: SystemConfigKey) -> JsonData:
        """同步锁定配置行并返回独立值副本。"""
        record = self._sync_session().execute(
            select(SystemConfig)
            .where(SystemConfig.key == key.value)
            .with_for_update()
        ).scalar_one_or_none()
        return copy.deepcopy(record.value if record is not None else None)

    async def async_get_for_update(self, key: SystemConfigKey) -> JsonData:
        """异步锁定配置行并返回独立值副本。"""
        result = await self._async_session().execute(
            select(SystemConfig)
            .where(SystemConfig.key == key.value)
            .with_for_update()
        )
        record = result.scalar_one_or_none()
        return copy.deepcopy(record.value if record is not None else None)

    def stage_set(self, key: SystemConfigKey, value: JsonData) -> None:
        """在同步 Session 中暂存配置值，不提交事务。"""
        session = self._sync_session()
        record = session.execute(
            select(SystemConfig).where(SystemConfig.key == key.value)
        ).scalar_one_or_none()
        if record is None:
            session.add(SystemConfig(key=key.value, value=copy.deepcopy(value)))
        else:
            record.value = copy.deepcopy(value)

    async def async_stage_set(
        self,
        key: SystemConfigKey,
        value: JsonData,
    ) -> None:
        """在 AsyncSession 中暂存配置值，不提交事务。"""
        session = self._async_session()
        result = await session.execute(
            select(SystemConfig).where(SystemConfig.key == key.value)
        )
        record = result.scalar_one_or_none()
        if record is None:
            session.add(SystemConfig(key=key.value, value=copy.deepcopy(value)))
        else:
            record.value = copy.deepcopy(value)


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
