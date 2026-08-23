from typing import Any, Optional
from sqlalchemy import String, JSON, Index, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import get_id_column, Base
from app.db.decorators import legacy_async_db_query, legacy_db_query
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


class PluginData(Base):
    """
    插件数据表
    """
    id = get_id_column()
    plugin_id: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, default=DEFAULT_INSTANCE_ID)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[Optional[Any]] = mapped_column(JSON)

    __table_args__ = (
        Index('ix_plugindata_plugin_id_instance_id_key', 'plugin_id', 'instance_id', 'key'),
    )

    @classmethod
    @legacy_db_query
    def get_plugin_data(
        cls,
        db: Session | None = None,
        plugin_id: str | None = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        """在调用方 Session 中读取某实例下插件全部数据，并兼容旧无会话入口。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识，默认取默认实例
        :return: 该实例下该插件的数据行列表
        """
        if plugin_id is None:
            raise TypeError("plugin_id is required")
        return list(db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        ).scalars().all())

    @classmethod
    @legacy_async_db_query
    async def async_get_plugin_data(
        cls,
        db: AsyncSession | None = None,
        plugin_id: str | None = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        """在调用方 AsyncSession 中读取某实例下插件全部数据，并兼容旧无会话入口。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识，默认取默认实例
        :return: 该实例下该插件的数据行列表
        """
        if plugin_id is None:
            raise TypeError("plugin_id is required")
        result = await db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        )
        return list(result.scalars().all())

    @classmethod
    @legacy_db_query
    def get_plugin_data_by_key(
        cls,
        db: Session | None = None,
        plugin_id: str | None = None,
        key: str | None = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        """在调用方 Session 中按键读取某实例下插件数据，并兼容旧无会话入口。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :param key: 数据键
        :param instance_id: 实例标识，默认取默认实例
        :return: 命中的数据行，不存在返回 None
        """
        if plugin_id is None or key is None:
            raise TypeError("plugin_id and key are required")
        return db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.key == key, cls.instance_id == instance_id)
        ).scalars().first()

    @classmethod
    @legacy_async_db_query
    async def async_get_plugin_data_by_key(
        cls,
        db: AsyncSession | None = None,
        plugin_id: str | None = None,
        key: str | None = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        """在调用方 AsyncSession 中按键读取某实例下插件数据，并兼容旧无会话入口。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :param key: 数据键
        :param instance_id: 实例标识，默认取默认实例
        :return: 命中的数据行，不存在返回 None
        """
        if plugin_id is None or key is None:
            raise TypeError("plugin_id and key are required")
        result = await db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.key == key, cls.instance_id == instance_id)
        )
        return result.scalar_one_or_none()

    @classmethod
    def del_plugin_data_by_key(cls, db: Session, plugin_id: str, key: str,
                                instance_id: Optional[str] = None):
        """
        在调用方事务中暂存单个插件键删除。

        与查询方法的默认范围刻意不对称：本方法默认 ``instance_id=None``，即跨该插件
        全部实例删除该键；显式传入实例标识时才收窄到那一个实例。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :param key: 数据键
        :param instance_id: 实例标识，为 None 时跨全部实例删除
        :return: 无返回值
        """
        statement = delete(cls).where(cls.plugin_id == plugin_id, cls.key == key)
        if instance_id is not None:
            statement = statement.where(cls.instance_id == instance_id)
        db.execute(statement)

    @classmethod
    def del_plugin_data(cls, db: Session, plugin_id: str, instance_id: Optional[str] = None):
        """
        在调用方事务中暂存插件全部数据删除。

        与查询方法的默认范围刻意不对称：本方法默认 ``instance_id=None``，即跨该插件
        全部实例整插件清空；显式传入实例标识时才只清那一个实例。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识，为 None 时跨全部实例删除
        :return: 无返回值
        """
        statement = delete(cls).where(cls.plugin_id == plugin_id)
        if instance_id is not None:
            statement = statement.where(cls.instance_id == instance_id)
        db.execute(statement)

    @classmethod
    @legacy_db_query
    def get_plugin_data_by_plugin_id(
        cls,
        db: Session | None = None,
        plugin_id: str | None = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        """在调用方 Session 中按插件 ID 读取某实例下插件全部数据，并兼容旧无会话入口。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识，默认取默认实例
        :return: 该实例下该插件的数据行列表
        """
        if plugin_id is None:
            raise TypeError("plugin_id is required")
        return list(db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        ).scalars().all())

    @classmethod
    @legacy_async_db_query
    async def async_get_plugin_data_by_plugin_id(
        cls,
        db: AsyncSession | None = None,
        plugin_id: str | None = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        """在调用方 AsyncSession 中按插件 ID 读取某实例下插件全部数据，并兼容旧无会话入口。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识，默认取默认实例
        :return: 该实例下该插件的数据行列表
        """
        if plugin_id is None:
            raise TypeError("plugin_id is required")
        result = await db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        )
        return list(result.scalars().all())
