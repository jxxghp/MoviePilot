from typing import Optional
from sqlalchemy import Integer, String, Boolean, DateTime, Text, select, ForeignKey, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column
from datetime import datetime

from app.db.base import Base, get_id_column


def _get_by_user_id_statement(model: type["PassKey"], user_id: int):
    """构造按用户筛选启用 PassKey 的查询语句。"""
    return select(model).where(model.user_id == user_id, model.is_active.is_(True))


def _get_by_credential_id_statement(
        model: type["PassKey"], credential_id: str,
):
    """构造按凭证 ID 筛选启用 PassKey 的查询语句。"""
    return select(model).where(
        model.credential_id == credential_id,
        model.is_active.is_(True),
    )


class PassKey(Base):
    """
    用户PassKey凭证表
    """
    # ID
    id = get_id_column()
    # 用户ID
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user.id'), nullable=False, index=True)
    # 凭证ID (credential_id)
    credential_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    # 凭证公钥
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    # 签名计数器
    sign_count: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 凭证名称（用户自定义）
    name: Mapped[Optional[str]] = mapped_column(String, default="通行密钥")
    # AAGUID (Authenticator Attestation GUID)
    aaguid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 创建时间
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.now)
    # 最后使用时间
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 是否启用
    is_active: Mapped[Optional[bool]] = mapped_column(Boolean, default=True)
    # 传输方式 (usb, nfc, ble, internal)
    transports: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    @classmethod
    def get_by_user_id(
            cls,
            db: Session,
            user_id: int,
    ):
        """在调用方 Session 中获取用户的所有启用 PassKey。"""
        return list(db.execute(
            _get_by_user_id_statement(cls, user_id)
        ).scalars().all())

    @classmethod
    async def async_get_by_user_id(cls, db: AsyncSession, user_id: int):
        """在调用方 AsyncSession 中获取用户的所有启用 PassKey。"""
        result = await db.execute(
            _get_by_user_id_statement(cls, user_id)
        )
        return list(result.scalars().all())

    @classmethod
    def get_by_credential_id(
            cls,
            db: Session,
            credential_id: str,
    ):
        """在调用方 Session 中按凭证 ID 获取启用 PassKey。"""
        return db.execute(
            _get_by_credential_id_statement(cls, credential_id)
        ).scalars().first()

    @classmethod
    async def async_get_by_credential_id(cls, db: AsyncSession, credential_id: str):
        """在调用方 AsyncSession 中根据凭证 ID 获取启用 PassKey。"""
        result = await db.execute(
            _get_by_credential_id_statement(cls, credential_id)
        )
        return result.scalars().first()

    @classmethod
    def get_by_id(cls, db: Session, passkey_id: int):
        """在调用方 Session 中根据 ID 获取 PassKey。"""
        return db.execute(select(cls).where(cls.id == passkey_id)).scalars().first()

    @classmethod
    async def async_get_by_id(cls, db: AsyncSession, passkey_id: int):
        """在调用方 AsyncSession 中根据 ID 获取 PassKey。"""
        result = await db.execute(
            select(cls).filter(cls.id == passkey_id)
        )
        return result.scalars().first()

    @classmethod
    def delete_by_id(cls, db: Session, passkey_id: int, user_id: int):
        """删除指定用户的PassKey"""
        passkey = db.execute(
            select(cls).where(cls.id == passkey_id, cls.user_id == user_id)
        ).scalars().first()
        if passkey:
            db.delete(passkey)
            return True
        return False

    @classmethod
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
            await db.delete(passkey)
            return True
        return False

    def update_last_used(self, db: Session, sign_count: int):
        """更新最后使用时间和签名计数"""
        db.execute(update(type(self)).where(type(self).id == self.id).values(
            last_used_at=datetime.now(),
            sign_count=sign_count,
        ))
        return True

    async def async_update_last_used(self, db: AsyncSession, sign_count: int):
        """异步更新最后使用时间和签名计数"""
        await db.execute(update(type(self)).where(type(self).id == self.id).values(
            last_used_at=datetime.now(),
            sign_count=sign_count,
        ))
        return True
