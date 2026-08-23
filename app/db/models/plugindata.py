from typing import Any, Optional
from sqlalchemy import String, JSON, Index, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import get_id_column, Base


class PluginData(Base):
    """
    插件数据表
    """
    id = get_id_column()
    plugin_id: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[Optional[Any]] = mapped_column(JSON)

    __table_args__ = (
        Index('ix_plugindata_plugin_id_key', 'plugin_id', 'key'),
    )

    @classmethod
    def get_plugin_data(cls, db: Session, plugin_id: str):
        """在调用方 Session 中读取插件全部数据。"""
        return list(db.execute(select(cls).where(cls.plugin_id == plugin_id)).scalars().all())

    @classmethod
    async def async_get_plugin_data(
        cls, db: AsyncSession, plugin_id: str
    ):
        """在调用方 AsyncSession 中读取插件全部数据。"""
        result = await db.execute(select(cls).where(cls.plugin_id == plugin_id))
        return list(result.scalars().all())

    @classmethod
    def get_plugin_data_by_key(
        cls, db: Session, plugin_id: str, key: str
    ):
        """在调用方 Session 中按键读取插件数据。"""
        return db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.key == key)
        ).scalars().first()

    @classmethod
    async def async_get_plugin_data_by_key(
        cls, db: AsyncSession, plugin_id: str, key: str
    ):
        """在调用方 AsyncSession 中按键读取插件数据。"""
        result = await db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.key == key)
        )
        return result.scalar_one_or_none()

    @classmethod
    def del_plugin_data_by_key(cls, db: Session, plugin_id: str, key: str):
        """在调用方事务中暂存单个插件键删除。"""
        db.execute(delete(cls).where(cls.plugin_id == plugin_id, cls.key == key))

    @classmethod
    def del_plugin_data(cls, db: Session, plugin_id: str):
        """在调用方事务中暂存插件全部数据删除。"""
        db.execute(delete(cls).where(cls.plugin_id == plugin_id))

    @classmethod
    def get_plugin_data_by_plugin_id(
        cls, db: Session, plugin_id: str
    ):
        """在调用方 Session 中按插件 ID 读取数据。"""
        return list(db.execute(select(cls).where(cls.plugin_id == plugin_id)).scalars().all())

    @classmethod
    async def async_get_plugin_data_by_plugin_id(
        cls, db: AsyncSession, plugin_id: str
    ):
        """在调用方 AsyncSession 中按插件 ID 读取数据。"""
        result = await db.execute(select(cls).where(cls.plugin_id == plugin_id))
        return list(result.scalars().all())
