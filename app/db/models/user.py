from sqlalchemy import Boolean, Column, JSON, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import Base, db_query, db_update, async_db_query, async_db_update, get_id_column


class User(Base):
    """
    用户表
    """
    # ID
    id = get_id_column()
    # 用户名，唯一值
    name = Column(String, index=True, nullable=False)
    # 邮箱
    email = Column(String)
    # 加密后密码
    hashed_password = Column(String)
    # 是否启用
    is_active = Column(Boolean(), default=True)
    # 是否管理员
    is_superuser = Column(Boolean(), default=False)
    # 头像
    avatar = Column(String)
    # 是否启用otp二次验证
    is_otp = Column(Boolean(), default=False)
    # otp秘钥
    otp_secret = Column(String, default=None)
    # 用户权限 json
    permissions = Column(JSON, default=dict)
    # 用户个性化设置 json
    settings = Column(JSON, default=dict)

    @classmethod
    @db_query
    def get_by_name(cls, db: Session, name: str):
        return db.query(cls).filter(cls.name == name).first()

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
        return db.query(cls).filter(cls.id == user_id).first()

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
