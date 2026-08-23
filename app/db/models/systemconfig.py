from typing import Any, Optional
from sqlalchemy import String, JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column


class SystemConfig(Base):
    """
    配置表
    """
    id = get_id_column()
    # 主键
    key: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 值
    value: Mapped[Optional[Any]] = mapped_column(JSON)

    @classmethod
    def get_by_key(cls, db: Session, key: str):
        """在调用方 Session 中按配置键查询系统配置。"""
        return db.execute(select(cls).where(cls.key == key)).scalars().first()

    @classmethod
    async def async_get_by_key(cls, db: AsyncSession, key: str):
        """在调用方 AsyncSession 中按配置键查询系统配置。"""
        result = await db.execute(select(cls).where(cls.key == key))
        return result.scalar_one_or_none()

    def delete_by_key(self, db: Session, key: str):
        """在调用方持有的事务中暂存指定配置删除。"""
        systemconfig = self.get_by_key(db, key)
        if systemconfig:
            db.delete(systemconfig)
        return True
