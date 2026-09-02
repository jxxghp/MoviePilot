"""用户应用端口的 SQLAlchemy 快照与事务适配器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Optional, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.security.user import (
    AuxiliaryUserCreate,
    ChainUserRepository,
    FrozenJson,
    LastActiveSuperuserError,
    UserAuthSnapshot,
    UserNameConflictError,
    UserRepository,
    UserSnapshot,
    UserUpdateResult,
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

    def get_by_name(self, name: str) -> Optional[UserSnapshot]:
        """在同步请求会话中按用户名读取冻结快照。"""
        model = self._oper.get_by_name(name)
        return _to_snapshot(model) if model else None

    def get_by_id(self, user_id: int) -> Optional[UserSnapshot]:
        """在同步请求会话中按 ID 读取冻结快照。"""
        model = self._oper.get_by_id(user_id)
        return _to_snapshot(model) if model else None

    def get_active_superuser(self) -> Optional[UserSnapshot]:
        """按主键顺序返回首个启用的超级管理员快照。"""
        session = cast(Session, self._session)
        model = session.execute(
            select(User)
            .where(User.is_active.is_(True), User.is_superuser.is_(True))
            .order_by(User.id)
            .limit(1)
        ).scalars().first()
        return _to_snapshot(model) if model else None

    async def async_has_users(self) -> bool:
        """使用最小列查询判断数据库中是否已有用户。"""
        session = self._require_async_session()
        result = await session.execute(select(User.id).limit(1))
        return result.scalar_one_or_none() is not None

    async def async_list(
        self,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> list[UserSnapshot]:
        """在异步请求会话中按可选窗口读取冻结用户快照。"""
        session = self._require_async_session()
        statement = select(User).order_by(User.id)
        if page is not None and count is not None:
            statement = statement.offset((page - 1) * count).limit(count)
        result = await session.execute(statement)
        return [_to_snapshot(model) for model in result.scalars().all()]

    async def async_count(self) -> int:
        """在异步请求会话中返回用户精确总数。"""
        session = self._require_async_session()
        result = await session.execute(select(func.count()).select_from(User))
        return int(result.scalar_one())

    async def async_get_by_name(self, name: str) -> Optional[UserSnapshot]:
        """在异步请求会话中按用户名读取冻结快照。"""
        model = await self._oper.async_get_by_name(name)
        return _to_snapshot(model) if model else None

    async def async_get_by_id(self, user_id: int) -> Optional[UserSnapshot]:
        """在异步请求会话中按 ID 读取冻结快照。"""
        model = await self._oper.async_get_by_id(user_id)
        return _to_snapshot(model) if model else None

    async def async_create(
        self,
        payload: dict[str, Any],
    ) -> Optional[UserSnapshot]:
        """在请求事务中暂存用户创建并返回冻结快照。"""
        session = self._require_async_session()
        model = User(**payload)
        session.add(model)
        try:
            await session.flush()
        except IntegrityError as error:
            raise UserNameConflictError(payload.get("name")) from error
        return _to_snapshot(model)

    async def async_update(
        self,
        user_id: int,
        payload: dict[str, Any],
    ) -> Optional[UserUpdateResult]:
        """原子更新用户；数据库外键负责按用户名级联偏好。"""
        session = self._require_async_session()
        model = await self._locked_user(session, user_id, payload)
        if model is None:
            return None
        old_name = model.name
        new_name = str(payload.get("name", old_name))
        values = {key: value for key, value in payload.items() if key != "id"}
        for key, value in values.items():
            setattr(model, key, value)
        try:
            await session.flush()
        except IntegrityError as error:
            raise UserNameConflictError(new_name) from error
        return UserUpdateResult(
            user=_to_snapshot(model),
            previous_name=old_name,
        )

    async def async_delete(self, user_id: int) -> Optional[str]:
        """原子删除用户；数据库外键负责级联偏好和 PassKey。"""
        session = self._require_async_session()
        model = await self._locked_user(session, user_id, None)
        if model is None:
            return None
        username: str = model.name
        await session.delete(model)
        await session.flush()
        return username

    async def async_update_otp_by_name(
        self,
        name: str,
        otp: bool,
        secret: str,
    ) -> None:
        """在请求事务中暂存用户 OTP 状态更新。"""
        await self._oper.async_update_otp_by_name(name, otp, secret)

    def _require_async_session(self) -> AsyncSession:
        """返回写用例要求的异步 Session，拒绝错误组合。"""
        if not isinstance(self._session, AsyncSession):
            raise RuntimeError("用户异步写入必须绑定 AsyncSession")
        return self._session

    @staticmethod
    async def _locked_user(
        session: AsyncSession,
        user_id: int,
        payload: Optional[dict[str, Any]],
    ) -> Optional[User]:
        """先锁管理员集合再锁目标用户，保护并发下最后一个启用管理员。"""
        result = await session.execute(
            select(User)
            .where(User.is_active.is_(True), User.is_superuser.is_(True))
            .order_by(User.id)
            .with_for_update()
        )
        administrators = list(result.scalars().all())
        model = next(
            (administrator for administrator in administrators if administrator.id == user_id),
            None,
        )
        if model is None:
            locked = await session.execute(select(User).where(User.id == user_id).with_for_update())
            model = cast(Optional[User], locked.scalars().first())
            if model is None:
                return None
        remains_active = (
            payload is not None
            and bool(payload.get("is_active", model.is_active))
            and bool(payload.get("is_superuser", model.is_superuser))
        )
        removes_active_superuser = bool(model.is_active and model.is_superuser and not remains_active)
        if removes_active_superuser and len(administrators) <= 1:
            raise LastActiveSuperuserError(model.name)
        return model


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

    def get_by_name(self, name: str) -> Optional[UserSnapshot]:
        """按用户名读取公开用户快照。"""
        with self._sync_session() as session:
            return SqlAlchemyUserRepository(session).get_by_name(name)

    def get_by_id(self, user_id: int) -> Optional[UserSnapshot]:
        """按 ID 读取公开用户快照。"""
        with self._sync_session() as session:
            return SqlAlchemyUserRepository(session).get_by_id(user_id)

    def get_active_superuser(self) -> Optional[UserSnapshot]:
        """在独立会话中返回首个启用的超级管理员快照。"""
        with self._sync_session() as session:
            return SqlAlchemyUserRepository(session).get_active_superuser()

    def get_auth_by_name(self, name: str) -> Optional[UserAuthSnapshot]:
        """按用户名读取认证凭据快照。"""
        with self._sync_session() as session:
            model = UserOper(db=session).get_by_name(name)
            return _to_auth_snapshot(model) if model else None

    async def async_get_by_name(self, name: str) -> Optional[UserSnapshot]:
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
    ) -> Optional[Mapping[str, FrozenJson]]:
        """同步读取用户通知设置的只读快照。"""
        user = self.get_by_name(name)
        return user.settings if user else None

    async def async_get_notification_settings(
        self,
        name: str,
    ) -> Optional[Mapping[str, FrozenJson]]:
        """异步读取用户通知设置的只读快照。"""
        user = await self.async_get_by_name(name)
        return user.settings if user else None

    def find_name_by_bindings(
        self,
        bindings: Mapping[str, object],
    ) -> Optional[str]:
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
