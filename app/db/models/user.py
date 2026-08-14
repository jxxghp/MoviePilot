from sqlalchemy import Boolean, JSON, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, mapped_column

from app.db import Base, db_query, db_update, async_db_query, async_db_update, get_id_column


class User(Base):
    """
    用户表
    """
    # ID
    id = get_id_column()
    # 用户名，唯一值
    name = mapped_column(String, index=True, nullable=False)
    # 邮箱
    email = mapped_column(String)
    # 加密后密码
    hashed_password = mapped_column(String)
    # 是否启用
    is_active = mapped_column(Boolean(), default=True)
    # 是否管理员
    is_superuser = mapped_column(Boolean(), default=False)
    # 头像
    avatar = mapped_column(String)
    # 是否启用otp二次验证
    is_otp = mapped_column(Boolean(), default=False)
    # otp秘钥
    otp_secret = mapped_column(String, default=None)
    # 用户权限 json
    permissions = mapped_column(JSON, default=dict)
    # 用户个性化设置 json
    settings = mapped_column(JSON, default=dict)

    @classmethod
    @db_query
    def get_by_name(cls, db: Session, name: str):
        return db.execute(select(cls).where(cls.name == name)).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_by_name(cls, db: AsyncSession, name: str):
        result = await db.execute(
            select(cls).filter(cls.name == name)
        )
        return result.scalars().first()

    @classmethod
    @db_query
    def get_by_id(cls, db: Session, user_id: int):
        return db.execute(select(cls).where(cls.id == user_id)).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_by_id(cls, db: AsyncSession, user_id: int):
        result = await db.execute(
            select(cls).filter(cls.id == user_id)
        )
        return result.scalars().first()

    @db_update
    def delete_by_name(self, db: Session, name: str):
        user = self.get_by_name(db, name)
        if user:
            user.delete(db, user.id)
        return True

    @async_db_update
    async def async_delete_by_name(self, db: AsyncSession, name: str):
        user = await self.async_get_by_name(db, name)
        if user:
            await user.async_delete(db, user.id)
        return True

    @db_update
    def delete_by_id(self, db: Session, user_id: int):
        user = self.get_by_id(db, user_id)
        if user:
            user.delete(db, user.id)
        return True

    @async_db_update
    async def async_delete_by_id(self, db: AsyncSession, user_id: int):
        user = await self.async_get_by_id(db, user_id)
        if user:
            await user.async_delete(db, user.id)
        return True

    @db_update
    def update_otp_by_name(self, db: Session, name: str, otp: bool, secret: str):
        user = self.get_by_name(db, name)
        if user:
            user.update(db, {
                'is_otp': otp,
                'otp_secret': secret
            })
            return True
        return False

    @async_db_update
    async def async_update_otp_by_name(self, db: AsyncSession, name: str, otp: bool, secret: str):
        user = await self.async_get_by_name(db, name)
        if user:
            await user.async_update(db, {
                'is_otp': otp,
                'otp_secret': secret
            })
            return True
        return False
