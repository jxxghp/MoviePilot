"""共享源码插件的实例描述符与配置持久化模型。"""

from __future__ import annotations

from typing import Any, List, Optional, Self, cast

from sqlalchemy import (
    JSON,
    Boolean,
    Index,
    String,
    UniqueConstraint,
    column,
    false,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column


class PluginInstance(Base):
    """持久化一个共享源码插件的运行实例，一实例一行。

    ``instance_id`` 既是分身的实例 ID，也可以等于 ``source_plugin_id`` 表示源插件
    本体自身；两者相等即本体，不等即分身，不再另设模式列——该列会是从这个等式复制
    出来的冗余副本，两者一旦失步就会让同一行在不同读取口被判成不同角色。

    身份（``instance_id`` 与 ``source_plugin_id``）之外的列都是这个实例的设置：
    展示信息与业务参数同属一个生命周期，应当随卸载一起消失、随重建一起重来，因此
    存放在同一行而非分表协调。

    业务参数存进 ``config_data`` 而不是 ``systemconfig`` 的 ``plugin.<实例ID>`` 单键：
    那个键由插件 ID 决定、条目数随安装量增长，混在系统设置里会把主程序自己的设置项
    淹掉；而且它存的其实是实例 ID，表里没有任何一列说得出它属于哪个插件，想列出
    某个插件的全部实例配置就只能靠字符串前缀去猜。

    ``log_level`` 为空表示该实例跟随全局日志等级；非空且未过期时覆盖全局等级，
    ``log_expires_at`` 为空表示覆盖不过期。过期判定在读取时惰性执行，实现见
    ``app.runtime.log``，库里只存原样设置，不存已折算的结果。

    ``is_default_target`` 标记该实例是否为所属源插件的默认调用目标，即外部调用只给
    插件 ID、没给实例 ID 时应当选中的那一行；与该实例是本体还是分身无关，两者都可能
    被选为默认调用目标。「同一源插件至多一个默认调用目标」这条不变量由
    ``ux_plugininstance_default_target`` 条件唯一索引在数据库层强制，只索引置位的行，
    不靠应用层纪律：置位要先清旧再置新，两条 DML 之间的窗口只有库层约束拦得住。

    表名由 ``Base`` 按类名自动派生为小写 ``plugininstance``。
    """

    id = get_id_column()
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_name: Mapped[Optional[str]] = mapped_column(String(255))
    plugin_desc: Mapped[Optional[str]] = mapped_column(String(255))
    plugin_icon: Mapped[Optional[str]] = mapped_column(String(255))
    # server_default 与迁移 DDL 保持一致：只写 Python 端 default 时 create_all 建出的
    # 表不带 DEFAULT，与迁移建出的表结构不同，alembic 会一直报出差异
    is_default_target: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    log_level: Mapped[Optional[str]] = mapped_column(String(16))
    log_expires_at: Mapped[Optional[str]] = mapped_column(String(40))
    config_data: Mapped[Optional[Any]] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint("instance_id", name="uq_plugininstance_instance_id"),
        Index("ix_plugininstance_source_plugin_id", "source_plugin_id"),
        # 条件谓词按方言各给一份：布尔列与 True 比较时 SQLite 编译为 ``IS 1``、
        # PostgreSQL 编译为 ``IS true``。谓词整个丢失会退化成「每个源插件只能有一行
        # 实例」，把插件分身整个锁死，因此不能只给一份共用
        Index(
            "ux_plugininstance_default_target",
            "source_plugin_id",
            unique=True,
            sqlite_where=column("is_default_target", Boolean).is_(True),
            postgresql_where=column("is_default_target", Boolean).is_(True),
        ),
    )

    @property
    def is_host(self) -> bool:
        """该行是否为源插件本体自身，而非共享其源码的分身。"""
        return bool(self.instance_id == self.source_plugin_id)

    @property
    def carries_only_identity(self) -> bool:
        """除一对身份列外是否已不承载任何设置。

        本体行会在插件首次存配置时被隐式建出，配置被删掉后若各列皆空就只剩身份列，
        留着会让「有哪些插件登记过本体设置」的枚举逐步失真，因而可以回收。分身行不
        适用：分身的存在本身就由这一行表达，清空设置不等于删除分身。
        """
        return not any(
            (
                self.config_data is not None,
                self.is_default_target,
                self.log_level,
                self.log_expires_at,
                self.plugin_name,
                self.plugin_desc,
                self.plugin_icon,
            )
        )

    @classmethod
    def get_by_instance_id(cls, db: Session, instance_id: str) -> Optional[Self]:
        """在调用方 Session 中按实例 ID 查询单行。"""
        return cast(
            Optional[Self],
            db.execute(select(cls).where(cls.instance_id == instance_id)).scalars().first(),
        )

    @classmethod
    async def async_get_by_instance_id(
        cls,
        db: AsyncSession,
        instance_id: str,
    ) -> Optional[Self]:
        """在调用方 AsyncSession 中按实例 ID 查询单行。"""
        result = await db.execute(select(cls).where(cls.instance_id == instance_id))
        return cast(Optional[Self], result.scalars().first())

    @classmethod
    def list_by_source_plugin_id(cls, db: Session, source_plugin_id: str) -> List[Self]:
        """在调用方 Session 中列出某个源插件名下的全部实例，含本体与各个分身。"""
        return cast(
            List[Self],
            list(
                db.execute(
                    select(cls).where(cls.source_plugin_id == source_plugin_id)
                ).scalars()
            ),
        )
