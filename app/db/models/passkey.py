from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, select, ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from datetime import datetime

from app.db import Base, db_query, db_update, async_db_query, async_db_update, get_id_column


class PassKey(Base):
    """
    用户PassKey凭证表
    """
    # ID
    id = get_id_column()
    # 用户ID
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False, index=True)
    # 凭证ID (credential_id)
    credential_id = Column(String, nullable=False, unique=True, index=True)
    # 凭证公钥
    public_key = Column(Text, nullable=False)
    # 签名计数器
    sign_count = Column(Integer, default=0)
    # 凭证名称（用户自定义）
    name = Column(String, default="通行密钥")
    # AAGUID (Authenticator Attestation GUID)
    aaguid = Column(String, nullable=True)
    # 创建时间
    created_at = Column(DateTime, default=datetime.now)
    # 最后使用时间
    last_used_at = Column(DateTime, nullable=True)
    # 是否启用
    is_active = Column(Boolean, default=True)
    # 传输方式 (usb, nfc, ble, internal)
    transports = Column(String, nullable=True)

    @classmethod
    @db_query
    def get_by_user_id(cls, db: Session, user_id: int):
        """获取用户的所有PassKey"""
        return db.query(cls).filter(cls.user_id == user_id, cls.is_active.is_(True)).all()

    @classmethod
    @async_db_query
    async def async_get_by_user_id(cls, db: AsyncSession, user_id: int):
        """异步获取用户的所有PassKey"""
        result = await db.execute(
            select(cls).filter(cls.user_id == user_id, cls.is_active.is_(True))
        )
        return result.scalars().all()

    @classmethod
    @db_query
    def get_by_credential_id(cls, db: Session, credential_id: str):
        """根据凭证ID获取PassKey"""
        return db.query(cls).filter(cls.credential_id == credential_id, cls.is_active.is_(True)).first()

    @classmethod
    @async_db_query
    async def async_get_by_credential_id(cls, db: AsyncSession, credential_id: str):
        """异步根据凭证ID获取PassKey"""
        result = await db.execute(
            select(cls).filter(cls.credential_id == credential_id, cls.is_active.is_(True))
        )
        return result.scalars().first()

    @classmethod
    @db_query
    def get_by_id(cls, db: Session, passkey_id: int):
        """根据ID获取PassKey"""
        return db.query(cls).filter(cls.id == passkey_id).first()

    @classmethod
    @async_db_query
    async def async_get_by_id(cls, db: AsyncSession, passkey_id: int):
        """异步根据ID获取PassKey"""
        result = await db.execute(
            select(cls).filter(cls.id == passkey_id)
        )
        return result.scalars().first()

    @classmethod
    @db_update
    def delete_by_id(cls, db: Session, passkey_id: int, user_id: int):
        """删除指定用户的PassKey"""
        passkey = db.query(cls).filter(
            cls.id == passkey_id,
            cls.user_id == user_id
        ).first()
        if passkey:
            passkey.delete(db, passkey.id)
            return True
        return False

    @classmethod
    @async_db_update
    async def async_delete_by_id(cls, db: AsyncSession, passkey_id: int, user_id: int):
        """异步删除指定用户的PassKey"""
        result = await db.execute(
            select(cls).filter(
                cls.id == passkey_id,
                cls.user_id == user_id
            )
        )
        passkey = result.scalars().first()
        if passkey:
            await passkey.async_delete(db, passkey.id)
            return True
        return False

    @db_update
    def update_last_used(self, db: Session, sign_count: int):
        """更新最后使用时间和签名计数"""
        self.update(db, {
            'last_used_at': datetime.now(),
            'sign_count': sign_count
        })
        return True

    @async_db_update
    async def async_update_last_used(self, db: AsyncSession, sign_count: int):
        """异步更新最后使用时间和签名计数"""
        await self.async_update(db, {
            'last_used_at': datetime.now(),
            'sign_count': sign_count
        })
        return True
