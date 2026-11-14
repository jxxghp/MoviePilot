"""
插件持久化数据的 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from typing import Any, Optional, Self

from sqlalchemy import JSON, Index, String, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column


class PluginData(Base):
    """
    插件数据表
    """
    id = get_id_column()
    plugin_id: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[Optional[Any]] = mapped_column(JSON)

    __table_args__ = (
        Index('ix_plugindata_plugin_id_key', 'plugin_id', 'key', unique=True),
    )

    @classmethod
    def get_plugin_data(cls, db: Session, plugin_id: str) -> list[Self]:
        """
        在调用方 Session 中读取插件全部数据。
        """
        return list(db.execute(select(cls).where(cls.plugin_id == plugin_id)).scalars().all())

    @classmethod
    async def async_get_plugin_data(
        cls, db: AsyncSession, plugin_id: str
    ) -> list[Self]:
        """
        在调用方 AsyncSession 中读取插件全部数据。
        """
        result = await db.execute(select(cls).where(cls.plugin_id == plugin_id))
        return list(result.scalars().all())

    @classmethod
    def get_plugin_data_by_key(
        cls, db: Session, plugin_id: str, key: str
    ) -> Optional[Self]:
        """
        在调用方 Session 中按键读取插件数据。
        """
        data: Optional[Self] = db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.key == key)
        ).scalars().first()
        return data

    @classmethod
    async def async_get_plugin_data_by_key(
        cls, db: AsyncSession, plugin_id: str, key: str
    ) -> Optional[Self]:
        """
        在调用方 AsyncSession 中按键读取插件数据。
        """
        result = await db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.key == key)
        )
        data: Optional[Self] = result.scalar_one_or_none()
        return data

    @classmethod
    def del_plugin_data_by_key(cls, db: Session, plugin_id: str, key: str) -> None:
        """
        在调用方事务中暂存单个插件键删除。
        """
        db.execute(delete(cls).where(cls.plugin_id == plugin_id, cls.key == key))

    @classmethod
    def del_plugin_data(cls, db: Session, plugin_id: str) -> None:
        """
        在调用方事务中暂存插件全部数据删除。
        """
        db.execute(delete(cls).where(cls.plugin_id == plugin_id))

    @classmethod
    def get_plugin_data_by_plugin_id(
        cls, db: Session, plugin_id: str
    ) -> list[Self]:
        """
        在调用方 Session 中按插件 ID 读取数据。
        """
        return list(db.execute(select(cls).where(cls.plugin_id == plugin_id)).scalars().all())

    @classmethod
    async def async_get_plugin_data_by_plugin_id(
        cls, db: AsyncSession, plugin_id: str
    ) -> list[Self]:
        """
        在调用方 AsyncSession 中按插件 ID 读取数据。
        """
        result = await db.execute(select(cls).where(cls.plugin_id == plugin_id))
        return list(result.scalars().all())
