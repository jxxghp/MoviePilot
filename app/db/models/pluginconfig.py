from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import Boolean, DateTime, Index, JSON, String, UniqueConstraint, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.decorators import async_db_query, async_db_update, db_query, db_update

# 日志等级合法取值；DEBUG 最详细，ERROR 最简略
LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


class PluginConfig(Base):
    """
    插件实例配置表。

    同一插件类可按配置扇出多个独立实例，每个实例由 ``(plugin_id, instance_id)``
    唯一确定；默认实例不做特例，同样占一行。

    ``plugin_version`` 记录该实例当前已生效（成功启动过）的版本，而非期望切换到
    的版本。期望版本由 ``follow_default_version`` 决定读取来源：为真时取默认实例
    （``instance_id`` 取默认值的那一行）的 ``plugin_version``，为假时就是本行自己
    的值。默认实例与本实例的版本不一致，本身即表达「待切换」，因此不需要再设一列
    「待生效版本」。

    ``plugin_version`` 只能在宿主按目标版本成功启动该实例之后写入；启动失败时保持
    原值不变，失败前的版本目录仍然存在，可直接以原版本重新启动完成回退。

    本表只接受 ``str`` 类型的实例标识，不做归一化或合法性校验——两者的单一真源在
    运行时扩展层，本表刻意对其无知。
    """
    id = get_id_column()
    # 插件标识（插件主类名）
    plugin_id: Mapped[str] = mapped_column(String, nullable=False)
    # 实例标识
    instance_id: Mapped[str] = mapped_column(String, nullable=False, default="default")
    # 该实例是否启用
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 该实例日志等级，取值见 LOG_LEVELS；NULL 表示跟随全局日志等级
    log_level: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 日志等级的失效时间，用于临时调高等级排障后自动恢复；NULL 表示不过期
    log_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 该实例的业务配置
    config_data: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # 该实例已生效的插件版本（原始版本号，非目录名）；NULL 表示尚未成功加载过任何版本
    plugin_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 是否跟随默认实例的版本
    follow_default_version: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 创建时间
    created_at: Mapped[Optional[str]] = mapped_column(String)
    # 更新时间
    updated_at: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("plugin_id", "instance_id", name="ux_pluginconfig_plugin_instance"),
        Index("ix_pluginconfig_plugin_id", "plugin_id"),
        Index("ix_pluginconfig_plugin_id_plugin_version", "plugin_id", "plugin_version"),
    )

    @classmethod
    @db_query
    def get_by_instance(cls, db: Session, plugin_id: str, instance_id: str) -> Optional["PluginConfig"]:
        """
        按插件标识与实例标识取单条配置。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :return: 命中的配置行，不存在返回 None
        """
        return db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        ).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_by_instance(
            cls, db: AsyncSession, plugin_id: str, instance_id: str
    ) -> Optional["PluginConfig"]:
        """
        异步按插件标识与实例标识取单条配置。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :return: 命中的配置行，不存在返回 None
        """
        result = await db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        )
        return result.scalars().first()

    @classmethod
    @db_query
    def list_by_plugin(cls, db: Session, plugin_id: str) -> List["PluginConfig"]:
        """
        列出某插件的全部实例配置。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :return: 该插件全部实例配置行
        """
        return list(db.execute(select(cls).where(cls.plugin_id == plugin_id)).scalars().all())

    @classmethod
    @async_db_query
    async def async_list_by_plugin(cls, db: AsyncSession, plugin_id: str) -> List["PluginConfig"]:
        """
        异步列出某插件的全部实例配置。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :return: 该插件全部实例配置行
        """
        result = await db.execute(select(cls).where(cls.plugin_id == plugin_id))
        return list(result.scalars().all())

    @classmethod
    @db_query
    def list_enabled(cls, db: Session) -> List["PluginConfig"]:
        """
        列出全部已启用的实例配置，不限插件。
        :param db: 数据库会话
        :return: 已启用的实例配置行
        """
        return list(db.execute(select(cls).where(cls.is_enabled.is_(True))).scalars().all())

    @classmethod
    @async_db_query
    async def async_list_enabled(cls, db: AsyncSession) -> List["PluginConfig"]:
        """
        异步列出全部已启用的实例配置，不限插件。
        :param db: 异步数据库会话
        :return: 已启用的实例配置行
        """
        result = await db.execute(select(cls).where(cls.is_enabled.is_(True)))
        return list(result.scalars().all())

    @classmethod
    @db_update
    def delete_by_instance(cls, db: Session, plugin_id: str, instance_id: str) -> int:
        """
        删除单个实例配置。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :return: 删除的行数
        """
        return execute_dml(
            db, delete(cls).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        )

    @classmethod
    @async_db_update
    async def async_delete_by_instance(cls, db: AsyncSession, plugin_id: str, instance_id: str) -> int:
        """
        异步删除单个实例配置。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 实例标识
        :return: 删除的行数
        """
        result = await db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        )
        row = result.scalars().first()
        if not row:
            return 0
        await db.delete(row)
        return 1

    @classmethod
    @db_update
    def delete_by_plugin(cls, db: Session, plugin_id: str) -> int:
        """
        删除某插件的全部实例配置。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :return: 删除的行数
        """
        return execute_dml(db, delete(cls).where(cls.plugin_id == plugin_id))

    @classmethod
    @async_db_update
    async def async_delete_by_plugin(cls, db: AsyncSession, plugin_id: str) -> int:
        """
        异步删除某插件的全部实例配置。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :return: 删除的行数
        """
        result = await db.execute(select(cls).where(cls.plugin_id == plugin_id))
        rows = list(result.scalars().all())
        for row in rows:
            await db.delete(row)
        return len(rows)
