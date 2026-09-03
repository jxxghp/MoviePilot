"""共享源码插件的实例描述符持久化模型。"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Index, String, UniqueConstraint, column
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class PluginInstanceDescriptor(Base):
    """持久化一个共享源码插件的运行实例描述，一实例一行。

    ``instance_id`` 既是分身的实例 ID，也可以等于 ``source_plugin_id`` 表示
    源插件本体的版本绑定；``mode`` 用来区分这两种角色，取值 ``virtual``（分身）
    或 ``host``（本体），互不进入对方的枚举视图。

    ``log_level`` 为空表示该实例跟随全局日志等级；非空且未过期时覆盖全局等级，
    过期判定见 ``app.runtime.log``，``log_expires_at`` 为空表示覆盖不过期。

    ``is_default_target`` 标记该实例是否为所属源插件的默认调用目标，即外部调用
    未指定实例时应当选中的那一行；与 ``mode``、``instance_id`` 是否等于
    ``source_plugin_id`` 都无关——本体和任意一个分身都可能被选为默认调用目标。
    「同一源插件至多一个默认调用目标」这条不变量由 ``ux_plugininstance_default_target``
    条件唯一索引在数据库层强制，只索引置位的行，不靠应用层纪律。
    """

    __tablename__ = "plugininstance"

    id = get_id_column()
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_name: Mapped[Optional[str]] = mapped_column(String(255))
    plugin_desc: Mapped[Optional[str]] = mapped_column(String(255))
    plugin_icon: Mapped[Optional[str]] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="virtual")
    plugin_version: Mapped[Optional[str]] = mapped_column(String(64))
    follow_current_version: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    log_level: Mapped[Optional[str]] = mapped_column(String(16))
    log_expires_at: Mapped[Optional[str]] = mapped_column(String(40))
    is_default_target: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint("instance_id", name="uq_plugininstance_instance_id"),
        Index("ix_plugininstance_source_plugin_id", "source_plugin_id"),
        CheckConstraint("mode IN ('virtual', 'host')", name="ck_plugininstance_mode"),
        Index(
            "ux_plugininstance_default_target",
            "source_plugin_id",
            unique=True,
            sqlite_where=column("is_default_target", Boolean).is_(True),
            postgresql_where=column("is_default_target", Boolean).is_(True),
        ),
    )
