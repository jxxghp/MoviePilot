"""
用户数据访问。

认证依赖已迁至 app/api/dependencies/auth.py——那是 HTTP 层的关注点，
产出认证与授权错误而非数据。本模块只保留 UserOper。

这里不为那八个名字留惰性转发，否则会把
app.db.oper.user -> app.api.dependencies.auth -> app.application.security 这条边永久焊进依赖图，
让数据访问模块在静态分析里牵着整个鉴权栈。仓外插件的旧 ``app.db.user_oper`` 路径由
runtime 兼容映射指向 SDK 薄门面；canonical 数据访问模块仍只依赖模型，不承担兼容职责。
"""
from typing import Any, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.user import User


class UserOper(DbOper):
    """
    用户管理
    """

    def list(self) -> List[User]:
        """
        获取用户列表
        """
        users: List[User] = self._execute_sync_query(User.list)
        return users

    def add(self, **kwargs: Any) -> None:
        """
        新增用户
        """
        user = User(**kwargs)
        self._stage_create(user)

    def get_by_name(self, name: str) -> Optional[User]:
        """
        根据用户名获取用户
        """
        return self._execute_sync_query(
            lambda session: User.get_by_name(session, name)
        )

    def get_by_id(self, user_id: int) -> Optional[User]:
        """
        按 ID 获取用户。
        """
        return self._execute_sync_query(
            lambda session: User.get_by_id(session, user_id)
        )

    def update(self, user_id: int, payload: dict[str, Any]) -> Optional[User]:
        """
        在调用方或独占短事务内重新查询并更新用户，不使用游离对象的旧会话。

        :param user_id: 要更新的用户主键
        :param payload: 本次要修改的用户字段
        :return: 更新后的用户；已删除时返回 None
        """
        def stage(session: Session) -> Optional[User]:
            """
            在同一有效会话内查询并暂存更新。
            """
            user = User.get_by_id(session, user_id)
            if user is not None:
                user.update(session, payload)
            return user

        return self._execute_sync_write(stage)

    async def async_list(self) -> List[User]:
        """
        异步获取用户列表。
        """
        users: List[User] = await self._execute_async_query(User.async_list)
        return users

    async def async_create(self, payload: dict[str, Any]) -> Optional[User]:
        """
        异步创建用户。
        """
        user = User(**payload)

        async def stage(session: AsyncSession) -> User:
            """
            在当前异步事务中暂存用户并分配主键。
            """
            session.add(user)
            await session.flush()
            return user

        return await self._execute_async_write(stage)

    async def async_update(self, user_id: int, payload: dict[str, Any]) -> Optional[User]:
        """
        异步更新用户。
        """
        user = await self.async_get_by_id(user_id)
        if user:
            async def stage(session: AsyncSession) -> User:
                """
                在当前事务中更新用户字段，必要时重新附加游离对象。
                """
                for key, value in payload.items():
                    setattr(user, key, value)
                updated_user: User = await session.merge(user)
                return updated_user

            await self._execute_async_write(stage)
        return user

    async def async_delete(self, user_id: int) -> bool:
        """
        异步删除用户。
        """
        return bool(await self._execute_async_write(
            lambda session: User.async_delete_by_id(session, user_id)
        ))

    async def async_delete_by_name(self, name: str) -> bool:
        """
        在独立异步事务中按用户名删除用户。
        """
        return bool(await self._execute_async_write(
            lambda session: User().async_delete_by_name(session, name)
        ))

    async def async_update_otp_by_name(
        self,
        name: str,
        otp: bool,
        secret: str,
    ) -> bool:
        """
        异步更新用户 OTP 状态。
        """
        return bool(await self._execute_async_write(
            lambda session: User.async_update_otp_by_name(
                session, name, otp, secret
            )
        ))

    async def async_get_by_name(self, name: str) -> Optional[User]:
        """
        异步根据用户名获取用户。
        """
        async def query(session: AsyncSession) -> Optional[User]:
            """
            在调用方异步会话中执行用户名查询。
            """
            result = await session.execute(select(User).where(User.name == name))
            found_user: Optional[User] = result.scalars().first()
            return found_user

        return await self._execute_async_query(query)

    async def async_get_by_id(self, user_id: int) -> Optional[User]:
        """
        异步根据用户 ID 获取用户。
        """
        async def query(session: AsyncSession) -> Optional[User]:
            """
            在调用方异步会话中执行用户 ID 查询。
            """
            result = await session.execute(select(User).where(User.id == user_id))
            found_user: Optional[User] = result.scalars().first()
            return found_user

        return await self._execute_async_query(query)

    def get_permissions(self, name: str) -> dict[str, Any]:
        """
        获取用户权限
        """
        user = self.get_by_name(name)
        if user:
            permissions: dict[str, Any] = user.permissions or {}
            return permissions
        return {}

    def get_settings(self, name: str) -> Optional[dict[str, Any]]:
        """
        获取用户个性化设置，返回None表示用户不存在
        """
        user = self.get_by_name(name)
        if user:
            settings: dict[str, Any] = user.settings or {}
            return settings
        return None

    def get_setting(self, name: str, key: str) -> Optional[str]:
        """
        获取用户个性化设置
        """
        settings = self.get_settings(name)
        if settings:
            return settings.get(key)
        return None

    def get_name(self, **kwargs: object) -> Optional[str]:
        """
        根据绑定账号获取用户名称
        """
        users = self.list()
        for user in users:
            user_setting = user.settings
            if user_setting:
                for k, v in kwargs.items():
                    if user_setting.get(k) == str(v):
                        user_name: str = user.name
                        return user_name
        return None
