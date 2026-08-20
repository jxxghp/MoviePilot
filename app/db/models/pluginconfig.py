from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import (Boolean, DateTime, Index, JSON, String, UniqueConstraint, column, delete,
                        select, update)
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
    的版本。期望版本由 ``follow_default_version`` 决定读取来源：非默认实例为真时
    取默认实例（``instance_id`` 取默认值的那一行）的 ``plugin_version``，为假时就是
    本行自己的值；默认实例那一行为真时取该插件当前安装的版本，为假时固定在本行
    自己的值——默认实例若也去读默认实例，会把它永远钉在上次生效的版本上，新装的
    版本再不生效。期望版本与已生效版本不一致，本身即表达「待切换」，因此不需要再设一列
    「待生效版本」。

    ``plugin_version`` 只能在宿主按目标版本成功启动该实例之后写入；启动失败时保持
    原值不变，失败前的版本目录仍然存在，可直接以原版本重新启动完成回退。

    本表只接受 ``str`` 类型的实例标识，不做归一化或合法性校验——两者的单一真源在
    运行时扩展层，本表刻意对其无知。

    表内有三处「默认」，语义互不相干：``instance_id`` 取 ``"default"`` 是身份默认，
    指未创建分身时那一个实例；``follow_default_version`` 是版本跟随，指期望版本读自
    身份默认那一行；``is_default_target`` 是调用目标默认，指外部调用未指定实例时该走
    哪一行，与前两者都无关——身份默认的实例不一定是调用目标，调用目标也可以是任一分身。
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
    # 该实例是否为本插件的默认调用目标，即外部调用未指定实例时选中的那一行
    is_default_target: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 创建时间
    created_at: Mapped[Optional[str]] = mapped_column(String)
    # 更新时间
    updated_at: Mapped[Optional[str]] = mapped_column(String)

    # 条件唯一索引把「每个插件至多一个默认调用目标」交给数据库判定：只索引置位的行，
    # 未置位的行不入索引，因而同一插件可以有任意多行为假、至多一行为真。应用层的
    # 「置新清旧」是同一事务内的顺序写，两个并发事务各自置位不同实例时都会通过应用层
    # 检查，只有这条索引能拦下后提交的那一个。部分索引的谓词是方言特性，SQLite 与
    # PostgreSQL 各给一份，两边渲染出的谓词分别是 ``IS 1`` 与 ``IS true``。
    __table_args__ = (
        UniqueConstraint("plugin_id", "instance_id", name="ux_pluginconfig_plugin_instance"),
        Index("ix_pluginconfig_plugin_id", "plugin_id"),
        Index("ix_pluginconfig_plugin_id_plugin_version", "plugin_id", "plugin_version"),
        Index(
            "ux_pluginconfig_default_target",
            "plugin_id",
            unique=True,
            sqlite_where=column("is_default_target", Boolean).is_(True),
            postgresql_where=column("is_default_target", Boolean).is_(True),
        ),
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
    @db_query
    def get_default_target(cls, db: Session, plugin_id: str) -> Optional["PluginConfig"]:
        """
        取某插件被置为默认调用目标的实例配置。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :return: 置位的配置行，未设置默认调用目标时返回 None
        """
        return db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.is_default_target.is_(True))
        ).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_default_target(cls, db: AsyncSession, plugin_id: str) -> Optional["PluginConfig"]:
        """
        异步取某插件被置为默认调用目标的实例配置。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :return: 置位的配置行，未设置默认调用目标时返回 None
        """
        result = await db.execute(
            select(cls).where(cls.plugin_id == plugin_id, cls.is_default_target.is_(True))
        )
        return result.scalars().first()

    @classmethod
    @db_update
    def set_default_target(cls, db: Session, plugin_id: str, instance_id: str) -> int:
        """
        把某插件的默认调用目标改为指定实例。

        目标实例不存在时原样返回，不动原有置位——先清后置一旦在目标缺席时执行到一半，
        结果是该插件从「有默认调用目标」变成「没有」，调用方却只看到一个失败返回值。
        目标存在时先清除同插件其余实例的置位再置位目标实例，两条 DML 处在同一事务内，
        中途不会出现两行同时为真；顺序反过来会先撞上条件唯一索引。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 要设为默认调用目标的实例标识
        :return: 置位的行数，目标实例没有配置行时为 0
        """
        target = db.execute(
            select(cls.id).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        ).first()
        if target is None:
            return 0
        execute_dml(
            db,
            update(cls)
            .where(
                cls.plugin_id == plugin_id,
                cls.instance_id != instance_id,
                cls.is_default_target.is_(True),
            )
            .values(is_default_target=False),
        )
        return execute_dml(
            db,
            update(cls)
            .where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
            .values(is_default_target=True),
        )

    @classmethod
    @async_db_update
    async def async_set_default_target(cls, db: AsyncSession, plugin_id: str, instance_id: str) -> int:
        """
        异步把某插件的默认调用目标改为指定实例，目标实例不存在时不动原有置位。
        :param db: 异步数据库会话
        :param plugin_id: 插件标识
        :param instance_id: 要设为默认调用目标的实例标识
        :return: 置位的行数，目标实例没有配置行时为 0
        """
        found = await db.execute(
            select(cls.id).where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
        )
        if found.first() is None:
            return 0
        await db.execute(
            update(cls)
            .where(
                cls.plugin_id == plugin_id,
                cls.instance_id != instance_id,
                cls.is_default_target.is_(True),
            )
            .values(is_default_target=False),
        )
        await db.execute(
            update(cls)
            .where(cls.plugin_id == plugin_id, cls.instance_id == instance_id)
            .values(is_default_target=True),
        )
        return 1

    @classmethod
    @db_update
    def clear_default_target(cls, db: Session, plugin_id: str) -> int:
        """
        清除某插件的默认调用目标置位，清除后该插件不再有默认调用目标。
        :param db: 数据库会话
        :param plugin_id: 插件标识
        :return: 清除的行数
        """
        return execute_dml(
            db,
            update(cls)
            .where(cls.plugin_id == plugin_id, cls.is_default_target.is_(True))
            .values(is_default_target=False),
        )

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
