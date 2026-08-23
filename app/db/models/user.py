from typing import Any, Optional
from sqlalchemy import Boolean, JSON, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.decorators import async_db_query, run_legacy_sync_query
from app.db.models.user_identity import UserIdentity


class User(Base):
    """
    用户表
    """
    # ID
    id = get_id_column()
    # 用户名，唯一值
    name: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # 邮箱
    email: Mapped[Optional[str]] = mapped_column(String)
    # 加密后密码
    hashed_password: Mapped[Optional[str]] = mapped_column(String)
    # 是否启用
    is_active: Mapped[Optional[bool]] = mapped_column(Boolean(), default=True)
    # 是否管理员
    is_superuser: Mapped[Optional[bool]] = mapped_column(Boolean(), default=False)
    # 头像
    avatar: Mapped[Optional[str]] = mapped_column(String)
    # 是否启用otp二次验证
    is_otp: Mapped[Optional[bool]] = mapped_column(Boolean(), default=False)
    # otp秘钥
    otp_secret: Mapped[Optional[str]] = mapped_column(String, default=None)
    # 用户权限 json
    permissions: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 用户个性化设置 json
    settings: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)

    @classmethod
    def get_by_name(
        cls,
        db: Session | str | None = None,
        name: str | None = None,
    ):
        """按用户名查询用户，兼容显式会话和旧插件无会话调用。"""
        if name is None and isinstance(db, str):
            name, db = db, None
        if name is None:
            raise TypeError("name is required")

        def query(session: Session):
            """在给定会话中执行用户名查询。"""
            return session.execute(select(cls).where(cls.name == name)).scalars().first()

        if isinstance(db, Session):
            return query(db)
        return run_legacy_sync_query(query)

    @classmethod
    @async_db_query
    async def async_get_by_name(cls, db: AsyncSession, name: str):
        result = await db.execute(
            select(cls).filter(cls.name == name)
        )
        return result.scalars().first()

    @classmethod
    def get_by_id(cls, db: Session | int | None = None, user_id: int | None = None):
        """按用户 ID 查询用户，兼容显式会话和旧插件无会话调用。"""
        if user_id is None and isinstance(db, int):
            user_id, db = db, None
        if user_id is None:
            raise TypeError("user_id is required")

        def query(session: Session):
            """在给定会话中执行用户 ID 查询。"""
            return session.execute(select(cls).where(cls.id == user_id)).scalars().first()

        if isinstance(db, Session):
            return query(db)
        return run_legacy_sync_query(query)

    @classmethod
    @async_db_query
    async def async_get_by_id(cls, db: AsyncSession, user_id: int):
        result = await db.execute(
            select(cls).filter(cls.id == user_id)
        )
        return result.scalars().first()

    def delete_by_name(self, db: Session, name: str):
        user = self.get_by_name(db, name)
        if user:
            UserIdentity.delete_by_user_id(db, user.id)
            db.delete(user)
        return True

    async def async_delete_by_name(self, db: AsyncSession, name: str):
        user = await self.async_get_by_name(db, name)
        if user:
            await UserIdentity.async_delete_by_user_id(db, user.id)
            await db.delete(user)
        return True

    def delete_by_id(self, db: Session, user_id: int):
        user = self.get_by_id(db, user_id)
        if user:
            # 数据库层已声明 user_id 外键 ON DELETE CASCADE，此处显式级联删除是因为
            # SQLite 默认不启用外键约束强制，不能只依赖数据库自动级联
            UserIdentity.delete_by_user_id(db, user_id)
            db.delete(user)
        return True

    @classmethod
    async def async_delete_by_id(cls, db: AsyncSession, user_id: int):
        """异步按用户 ID 删除用户，供 UserOper 通过类方法调用。"""
        user = await cls.async_get_by_id(db, user_id)
        if user:
            await UserIdentity.async_delete_by_user_id(db, user_id)
            await db.delete(user)
        return True

    def update_otp_by_name(self, db: Session, name: str, otp: bool, secret: str):
        user = self.get_by_name(db, name)
        if user:
            user.is_otp = otp
            user.otp_secret = secret
            return True
        return False

    @classmethod
    async def async_update_otp_by_name(cls, db: AsyncSession, name: str, otp: bool, secret: str):
        """异步按用户名更新 OTP 状态，供 UserOper 通过类方法调用。"""
        user = await cls.async_get_by_name(db, name)
        if user:
            user.is_otp = otp
            user.otp_secret = secret
            return True
        return False
