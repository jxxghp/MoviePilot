"""用户应用端口的 SQLAlchemy 快照与事务适配器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.security.user import (
    AuxiliaryUserCreate,
    ChainUserRepository,
    FrozenJson,
    UserAuthSnapshot,
    UserRepository,
    UserSnapshot,
)
from app.db.models.user import User
from app.db.oper.user import UserOper
from app.db.uow import SqlAlchemyUnitOfWork


def _to_snapshot(model: User) -> UserSnapshot:
    """在 ORM 会话仍有效时复制公开用户字段。"""
    return UserSnapshot.build(
        user_id=model.id,
        name=model.name,
        email=model.email,
        is_active=model.is_active,
        is_superuser=model.is_superuser,
        avatar=model.avatar,
        is_otp=model.is_otp,
        permissions=model.permissions,
        settings=model.settings,
    )


def _to_auth_snapshot(model: User) -> UserAuthSnapshot:
    """在 ORM 会话仍有效时复制认证凭据和公开资料。"""
    return UserAuthSnapshot(
        user=_to_snapshot(model),
        hashed_password=model.hashed_password,
        otp_secret=model.otp_secret,
    )


class SqlAlchemyUserRepository(UserRepository):
    """把请求级同步或异步 Session 适配为冻结用户仓储。"""

    def __init__(self, session: Session | AsyncSession) -> None:
        """保存请求拥有的 Session；提交与回滚仍由请求 UoW 负责。"""
        self._session = session
        self._oper = UserOper(db=session)

    def get_by_name(self, name: str) -> UserSnapshot | None:
        """在同步请求会话中按用户名读取冻结快照。"""
        model = self._oper.get_by_name(name)
        return _to_snapshot(model) if model else None

    def get_by_id(self, user_id: int) -> UserSnapshot | None:
        """在同步请求会话中按 ID 读取冻结快照。"""
        model = self._oper.get_by_id(user_id)
        return _to_snapshot(model) if model else None

    async def async_list(self) -> list[UserSnapshot]:
        """在异步请求会话中读取全部冻结用户快照。"""
        return [_to_snapshot(model) for model in await self._oper.async_list()]

    async def async_get_by_name(self, name: str) -> UserSnapshot | None:
        """在异步请求会话中按用户名读取冻结快照。"""
        model = await self._oper.async_get_by_name(name)
        return _to_snapshot(model) if model else None

    async def async_get_by_id(self, user_id: int) -> UserSnapshot | None:
        """在异步请求会话中按 ID 读取冻结快照。"""
        model = await self._oper.async_get_by_id(user_id)
        return _to_snapshot(model) if model else None

    async def async_create(self, payload: dict[str, Any]) -> UserSnapshot | None:
        """在请求事务中暂存用户创建并返回冻结快照。"""
        model = await self._oper.async_create(payload)
        return _to_snapshot(model) if model else None

    async def async_update(
        self,
        user_id: int,
        payload: dict[str, Any],
    ) -> UserSnapshot | None:
        """在请求事务中暂存用户更新并返回更新后的冻结快照。"""
        model = await self._oper.async_update(user_id, payload)
        return _to_snapshot(model) if model else None

    async def async_delete(self, user_id: int) -> None:
        """在请求事务中暂存用户删除。"""
        await self._oper.async_delete(user_id)

    async def async_update_otp_by_name(
        self,
        name: str,
        otp: bool,
        secret: str,
    ) -> None:
        """在请求事务中暂存用户 OTP 状态更新。"""
        await self._oper.async_update_otp_by_name(name, otp, secret)


class TransactionalUserRepository(ChainUserRepository):
    """为 Chain、Agent 和进程级认证提供短生命周期用户会话。"""

    def __init__(
        self,
        *,
        sync_session: Callable[[], Session],
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存同步与异步会话工厂，每次操作独占一个 Session。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def get_by_name(self, name: str) -> UserSnapshot | None:
        """按用户名读取公开用户快照。"""
        with self._sync_session() as session:
            return SqlAlchemyUserRepository(session).get_by_name(name)

    def get_by_id(self, user_id: int) -> UserSnapshot | None:
        """按 ID 读取公开用户快照。"""
        with self._sync_session() as session:
            return SqlAlchemyUserRepository(session).get_by_id(user_id)

    def get_auth_by_name(self, name: str) -> UserAuthSnapshot | None:
        """按用户名读取认证凭据快照。"""
        with self._sync_session() as session:
            model = UserOper(db=session).get_by_name(name)
            return _to_auth_snapshot(model) if model else None

    async def async_get_by_name(self, name: str) -> UserSnapshot | None:
        """异步按用户名读取公开用户快照。"""
        async with self._async_session() as session:
            return await SqlAlchemyUserRepository(session).async_get_by_name(name)

    def create_auxiliary(self, command: AuxiliaryUserCreate) -> UserAuthSnapshot:
        """在独占事务中创建辅助认证用户，提交失败时完整回滚。"""
        with self._sync_session() as session:
            session.expire_on_commit = False
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                model = User(
                    name=command.name,
                    hashed_password=command.hashed_password,
                    is_active=command.is_active,
                    is_superuser=command.is_superuser,
                )
                session.add(model)
                session.flush()
                snapshot = _to_auth_snapshot(model)
                unit_of_work.commit()
                return snapshot
            except Exception:
                unit_of_work.rollback()
                raise

    def get_notification_settings(
        self,
        name: str,
    ) -> Mapping[str, FrozenJson] | None:
        """同步读取用户通知设置的只读快照。"""
        user = self.get_by_name(name)
        return user.settings if user else None

    async def async_get_notification_settings(
        self,
        name: str,
    ) -> Mapping[str, FrozenJson] | None:
        """异步读取用户通知设置的只读快照。"""
        user = await self.async_get_by_name(name)
        return user.settings if user else None

    def find_name_by_bindings(self, bindings: Mapping[str, object]) -> str | None:
        """仅在全部绑定唯一匹配同一启用用户时返回用户名。"""
        if not bindings:
            return None
        expected = {key: str(value) for key, value in bindings.items()}
        with self._sync_session() as session:
            matches = {
                model.name
                for model in UserOper(db=session).list()
                if model.is_active
                and model.settings
                and all(model.settings.get(key) == value for key, value in expected.items())
            }
        return next(iter(matches)) if len(matches) == 1 else None
