from typing import Any, Iterable, List, Optional

from sqlalchemy import (Boolean, Index, JSON, String, UniqueConstraint, column, delete,
                        select, update)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.decorators import db_query, db_update

# 内建类型的提供方保留值。冒号不可能出现在扩展实例键里——扩展标识取插件主类名，
# 是 Python 标识符；实例键形如 ``扩展标识@实例标识``，冒号至多出现在 ``@`` 之后——
# 因此该取值与任何合法扩展标识天然不冲突。用保留值而非留空，是为了让「内建也可禁用」
# 那天仍有一个可判定的提供方，而不是退回布尔式的「是不是插件」。
BUILTIN_PROVIDER = "host:builtin"

# 允许经通用更新入口改写的列。身份三元组里的 capability/type 换掉即是另一行配置，
# is_default_target 的「清旧再置新」有专用入口，两者都不放进来。
UPDATABLE_FIELDS = frozenset({"name", "enabled", "config", "host_config", "provider"})


class ServiceConfig(Base):
    """
    服务实例配置表。

    一族服务（下载器、媒体服务器、消息渠道等）下有多个类型，一个类型可按用户配置
    扇出多个具名实例，每个实例由 ``(capability, type, name)`` 唯一确定。

    ``provider`` 记录该类型由谁提供，是记账而不是判据：一条配置该不该生效，判据
    永远是服务实例登记表当下有没有这个 ``(capability, type)``，与本列无关。本列只
    在类型查不到时被读一次，把「没有这个类型」翻译成「该类型由扩展 X 提供，X 当前
    未启用」。让它参与生效判定会造出第二个事实源，两边一漂移就是静默错误。

    因此本列失效（扩展重装、改名）只影响提示文案，不影响配置生效——配置与类型的
    连接键是 ``(capability, type)``，不是 ``provider``。这一点与第三方身份绑定表
    相反：那里 ``provider`` 是唯一键的一部分，失效即丢绑定。同理本列不进唯一约束，
    ``UNIQUE(capability, type, name)`` 必须跨 provider 生效，否则扩展换个标识重装，
    同名配置就会变成两条，用户会看到两个一模一样的下载器。

    实例级字段按消费方分两列：``config`` 由类型实现自己读，形状归该类型的配置契约管；
    ``host_config`` 由宿主读（整理逻辑读路径映射、消息路由读场景开关、调度读同步间隔），
    形状归宿主定义，不进配置契约。宿主侧取一个 JSON 列而不是逐字段建专属列，判据是
    「将来再多一个宿主消费的实例级字段要不要改表结构」——专属列每加一个字段就是一次
    迁移，而这类字段随宿主功能演进，不该把表结构绑在上面；同时它们又不能混进 ``config``，
    否则声明了 ``additionalProperties: false`` 的类型会把宿主自己的字段判为违约。

    内建类型的 ``provider`` 取保留值 ``BUILTIN_PROVIDER``，不留空。
    """
    id = get_id_column()
    # 族标识（downloader / mediaserver / notification / ...）
    capability: Mapped[str] = mapped_column(String, nullable=False)
    # 类型标识（qbittorrent / emby / ...）
    type: Mapped[str] = mapped_column(String, nullable=False)
    # 实例名，用户自填
    name: Mapped[str] = mapped_column(String, nullable=False)
    # 该实例是否启用
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 类型专属配置载荷，由类型实现自己消费，形状受该类型声明的配置契约约束
    config: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # 宿主消费的实例级字段载荷（路径映射、场景开关、同步媒体库与同步间隔等）
    host_config: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # 该实例是否为本族的默认调用目标，即外部调用未指定实例时选中的那一行
    is_default_target: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 提供该类型的扩展标识；内建类型取 BUILTIN_PROVIDER
    provider: Mapped[str] = mapped_column(String, nullable=False, default=BUILTIN_PROVIDER)

    # 唯一约束自带的索引已按最左前缀覆盖「列出某族全部配置」与「按 (族, 类型) 取配置」
    # 两类查询，因此不再为这两列另建索引。额外只要两条：一条按提供方筛出「提供方已
    # 消失」的配置，即加 provider 列的直接目的；一条条件唯一索引把「每族至多一个默认
    # 调用目标」交给数据库判定——只索引置位的行，未置位的行不入索引，同一族因而可以
    # 有任意多行为假、至多一行为真。应用层的「置新清旧」是同一事务内的顺序写，两个
    # 并发事务各自置位不同实例时都会通过应用层检查，只有这条索引能拦下后提交的那一个。
    # 部分索引的谓词是方言特性，SQLite 与 PostgreSQL 各给一份，两边渲染出的谓词分别
    # 是 ``IS 1`` 与 ``IS true``。
    __table_args__ = (
        UniqueConstraint(
            "capability", "type", "name", name="ux_serviceconfig_capability_type_name"
        ),
        Index("ix_serviceconfig_provider", "provider"),
        Index(
            "ux_serviceconfig_default_target",
            "capability",
            unique=True,
            sqlite_where=column("is_default_target", Boolean).is_(True),
            postgresql_where=column("is_default_target", Boolean).is_(True),
        ),
    )

    @classmethod
    @db_query
    def list_by_capability(cls, db: Session, capability: str) -> List["ServiceConfig"]:
        """
        列出某族的全部实例配置，按主键升序即写入先后。
        :param db: 数据库会话
        :param capability: 族标识
        :return: 该族全部实例配置行
        """
        return list(
            db.execute(
                select(cls).where(cls.capability == capability).order_by(cls.id)
            ).scalars().all()
        )

    @classmethod
    @db_query
    def list_by_type(cls, db: Session, capability: str, service_type: str) -> List["ServiceConfig"]:
        """
        列出某族某类型的全部实例配置，按主键升序即写入先后。
        :param db: 数据库会话
        :param capability: 族标识
        :param service_type: 类型标识
        :return: 该类型全部实例配置行
        """
        return list(
            db.execute(
                select(cls)
                .where(cls.capability == capability, cls.type == service_type)
                .order_by(cls.id)
            ).scalars().all()
        )

    @classmethod
    @db_query
    def get_by_identity(
            cls, db: Session, capability: str, service_type: str, name: str
    ) -> Optional["ServiceConfig"]:
        """
        按 ``(capability, type, name)`` 取单条实例配置。
        :param db: 数据库会话
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 命中的配置行，不存在返回 None
        """
        return db.execute(
            select(cls).where(
                cls.capability == capability, cls.type == service_type, cls.name == name
            )
        ).scalars().first()

    @classmethod
    @db_query
    def list_by_provider(cls, db: Session, provider: str) -> List["ServiceConfig"]:
        """
        列出某提供方名下的全部实例配置，不限族。
        :param db: 数据库会话
        :param provider: 提供方标识
        :return: 该提供方名下的实例配置行
        """
        return list(
            db.execute(
                select(cls).where(cls.provider == provider).order_by(cls.id)
            ).scalars().all()
        )

    @classmethod
    @db_query
    def list_with_absent_provider(
            cls, db: Session, present_providers: Iterable[str]
    ) -> List["ServiceConfig"]:
        """
        列出提供方已不在场的实例配置，内建保留值恒视为在场。
        :param db: 数据库会话
        :param present_providers: 当前在场的提供方标识集合
        :return: 提供方已消失的实例配置行
        """
        # 排序后再入 IN 列表：集合的迭代顺序不稳定，会让同一次查询渲染出不同的 SQL 文本
        known = tuple(sorted({BUILTIN_PROVIDER, *present_providers}))
        return list(
            db.execute(
                select(cls).where(cls.provider.notin_(known)).order_by(cls.id)
            ).scalars().all()
        )

    @classmethod
    @db_query
    def get_default_target(cls, db: Session, capability: str) -> Optional["ServiceConfig"]:
        """
        取某族被置为默认调用目标的实例配置。
        :param db: 数据库会话
        :param capability: 族标识
        :return: 置位的配置行，该族未设置默认调用目标时返回 None
        """
        return db.execute(
            select(cls).where(cls.capability == capability, cls.is_default_target.is_(True))
        ).scalars().first()

    @classmethod
    @db_update
    def set_default_target(
            cls, db: Session, capability: str, service_type: str, name: str
    ) -> int:
        """
        把某族的默认调用目标改为指定实例。

        目标实例不存在时原样返回，不动原有置位——先清后置一旦在目标缺席时执行到一半，
        结果是该族从「有默认调用目标」变成「没有」，调用方却只看到一个失败返回值。
        目标存在时先清除同族其余实例的置位再置位目标实例，两条 DML 处在同一事务内，
        中途不会出现两行同时为真；顺序反过来会先撞上条件唯一索引。
        :param db: 数据库会话
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 置位的行数，目标实例没有配置行时为 0
        """
        target = db.execute(
            select(cls.id).where(
                cls.capability == capability, cls.type == service_type, cls.name == name
            )
        ).first()
        if target is None:
            return 0
        execute_dml(
            db,
            update(cls)
            .where(
                cls.capability == capability,
                cls.id != target[0],
                cls.is_default_target.is_(True),
            )
            .values(is_default_target=False),
        )
        return execute_dml(
            db,
            update(cls).where(cls.id == target[0]).values(is_default_target=True),
        )

    @classmethod
    @db_update
    def clear_default_target(cls, db: Session, capability: str) -> int:
        """
        清除某族的默认调用目标置位，清除后该族不再有默认调用目标。
        :param db: 数据库会话
        :param capability: 族标识
        :return: 清除的行数
        """
        return execute_dml(
            db,
            update(cls)
            .where(cls.capability == capability, cls.is_default_target.is_(True))
            .values(is_default_target=False),
        )

    @classmethod
    @db_update
    def update_by_identity(
            cls, db: Session, capability: str, service_type: str, name: str, payload: dict
    ) -> int:
        """
        按 ``(capability, type, name)`` 更新单条实例配置。
        :param db: 数据库会话
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :param payload: 待写入的列值，键取自 ``UPDATABLE_FIELDS``
        :return: 更新的行数，无可写列或目标不存在时为 0
        """
        values = {key: value for key, value in payload.items() if key in UPDATABLE_FIELDS}
        if not values:
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(cls.capability == capability, cls.type == service_type, cls.name == name)
            .values(**values),
        )

    @classmethod
    @db_update
    def replace_capability(
            cls, db: Session, capability: str, records: List[dict]
    ) -> int:
        """
        用给定的整族配置覆盖某族现有配置。

        置位顺序是先清后置：整族的默认置位先清空，逐行写入时一律不置位，最后再置位
        选中的那一行。反过来做会在写入途中出现两行同时为真，直接撞上条件唯一索引。
        整个覆盖在一次事务内完成，中途失败不会留下写了一半的族。

        身份未变的行原地更新而不是删了重建：主键稳定，别处按 id 记账时不会因为一次
        保存就全部失配。
        :param db: 数据库会话
        :param capability: 族标识
        :param records: 该族的全部配置行，每项含 type/name/enabled/config/host_config/
            is_default_target/provider
        :return: 覆盖后该族的配置行数
        """
        existing = {
            (row.type, row.name): row
            for row in db.execute(
                select(cls).where(cls.capability == capability)
            ).scalars().all()
        }
        desired = {(record["type"], record["name"]): record for record in records}
        execute_dml(
            db,
            update(cls)
            .where(cls.capability == capability, cls.is_default_target.is_(True))
            .values(is_default_target=False),
        )
        for key, row in existing.items():
            if key not in desired:
                execute_dml(db, delete(cls).where(cls.id == row.id))
        default_target: Optional[tuple] = None
        for key, record in desired.items():
            # 调用方本应只交出至多一条置位，多交时取第一条：条件唯一索引只允许一行为真，
            # 取最后一条会让同一份输入按字典序的偶然顺序落到不同的行上
            if record.get("is_default_target") and default_target is None:
                default_target = key
            row = existing.get(key)
            # 调用方给不出提供方时沿用该行原有的记账，只有全新的行才落到内建保留值：
            # 提供该类型的扩展当前未启用时登记表本就查不到它，此时把 provider 抹成内建，
            # 「提供方已消失」这条提示就再也筛不出这一行——而这正是加这一列的目的
            values = {
                "enabled": bool(record.get("enabled")),
                "config": record.get("config"),
                "host_config": record.get("host_config"),
                "provider": (
                    record.get("provider")
                    or (row.provider if row is not None else None)
                    or BUILTIN_PROVIDER
                ),
            }
            if row is not None:
                execute_dml(db, update(cls).where(cls.id == row.id).values(**values))
                continue
            db.add(
                cls(
                    capability=capability,
                    type=key[0],
                    name=key[1],
                    is_default_target=False,
                    **values,
                )
            )
        db.flush()
        if default_target is not None:
            execute_dml(
                db,
                update(cls)
                .where(
                    cls.capability == capability,
                    cls.type == default_target[0],
                    cls.name == default_target[1],
                )
                .values(is_default_target=True),
            )
        return len(desired)

    @classmethod
    @db_update
    def delete_by_identity(
            cls, db: Session, capability: str, service_type: str, name: str
    ) -> int:
        """
        删除单条实例配置。
        :param db: 数据库会话
        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 删除的行数
        """
        return execute_dml(
            db,
            delete(cls).where(
                cls.capability == capability, cls.type == service_type, cls.name == name
            ),
        )
