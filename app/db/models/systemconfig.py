from typing import Any, Optional
from sqlalchemy import String, JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.decorators import db_query, db_update, async_db_query


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
    @db_query
    def get_by_key(cls, db: Session, key: str):
        return db.execute(select(cls).where(cls.key == key)).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_by_key(cls, db: AsyncSession, key: str):
        result = await db.execute(select(cls).where(cls.key == key))
        return result.scalar_one_or_none()

    @db_update
    def delete_by_key(self, db: Session, key: str):
        systemconfig = self.get_by_key(db, key)
        if systemconfig:
            systemconfig.delete(db, systemconfig.id)
        return True
