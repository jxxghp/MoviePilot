"""
用户账号、认证信息与权限的 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from typing import Any, Optional, Self
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Index, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column


class User(Base):
    """
    用户表
    """

    # 用户记录的数据库主键。
    id = get_id_column()
    # 用户名，唯一值
    name: Mapped[str] = mapped_column(String, nullable=False)
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
    # 通知使用不可变账号身份；主键复用、改名或删除重建不能继承旧设备。
    notification_identity: Mapped[str] = mapped_column(String(32), default=lambda: uuid4().hex, nullable=False)

    __table_args__ = (Index("ux_user_name", "name", unique=True),)

    @classmethod
    def get_by_name(
        cls,
        db: Session,
        name: str,
    ) -> Optional[Self]:
        """
        在调用方同步会话中按用户名查询用户。
        """
        user: Optional[Self] = db.execute(select(cls).where(cls.name == name)).scalars().first()
        return user

    @classmethod
    async def async_get_by_name(
        cls,
        db: AsyncSession,
        name: str,
    ) -> Optional[Self]:
        """
        在调用方异步会话中按用户名查询用户。
        """
        result = await db.execute(select(cls).filter(cls.name == name))
        user: Optional[Self] = result.scalars().first()
        return user

    @classmethod
    def get_by_id(cls, db: Session, user_id: int) -> Optional[Self]:
        """
        在调用方同步会话中按用户 ID 查询用户。
        """
        user: Optional[Self] = db.execute(select(cls).where(cls.id == user_id)).scalars().first()
        return user

    @classmethod
    async def async_get_by_id(
        cls,
        db: AsyncSession,
        user_id: int,
    ) -> Optional[Self]:
        """
        在调用方异步会话中按用户 ID 查询用户。
        """
        result = await db.execute(select(cls).filter(cls.id == user_id))
        user: Optional[Self] = result.scalars().first()
        return user

    def delete_by_name(self, db: Session, name: str) -> bool:
        """
        在调用方同步会话中按用户名暂存删除。
        """
        user = self.get_by_name(db, name)
        if user:
            db.delete(user)
        return True

    async def async_delete_by_name(self, db: AsyncSession, name: str) -> bool:
        """
        在调用方异步会话中按用户名暂存删除。
        """
        user = await self.async_get_by_name(db, name)
        if user:
            await db.delete(user)
        return True

    def delete_by_id(self, db: Session, user_id: int) -> bool:
        """
        在调用方同步会话中按用户 ID 暂存删除。
        """
        user = self.get_by_id(db, user_id)
        if user:
            db.delete(user)
        return True

    @classmethod
    async def async_delete_by_id(cls, db: AsyncSession, user_id: int) -> bool:
        """
        异步按用户 ID 删除用户，供 UserOper 通过类方法调用。
        """
        user = await cls.async_get_by_id(db, user_id)
        if user:
            await db.delete(user)
        return True

    def update_otp_by_name(self, db: Session, name: str, otp: bool, secret: str) -> bool:
        """
        在调用方同步会话中更新指定用户的 OTP 状态。
        """
        user = self.get_by_name(db, name)
        if user:
            user.is_otp = otp
            user.otp_secret = secret
            return True
        return False

    @classmethod
    async def async_update_otp_by_name(cls, db: AsyncSession, name: str, otp: bool, secret: str) -> bool:
        """
        异步按用户名更新 OTP 状态，供 UserOper 通过类方法调用。
        """
        user = await cls.async_get_by_name(db, name)
        if user:
            user.is_otp = otp
            user.otp_secret = secret
            return True
        return False
