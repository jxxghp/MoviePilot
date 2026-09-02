"""共享源码插件的实例描述符持久化模型。"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class PluginInstanceDescriptor(Base):
    """持久化一个共享源码插件的运行实例描述，一实例一行。

    ``instance_id`` 既是分身的实例 ID，也可以等于 ``source_plugin_id`` 表示
    源插件本体的版本绑定；``mode`` 用来区分这两种角色，取值 ``virtual``（分身）
    或 ``host``（本体），互不进入对方的枚举视图。
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
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint("instance_id", name="uq_plugininstance_instance_id"),
        Index("ix_plugininstance_source_plugin_id", "source_plugin_id"),
        CheckConstraint("mode IN ('virtual', 'host')", name="ck_plugininstance_mode"),
    )
