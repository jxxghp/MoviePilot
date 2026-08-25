"""插件安装事务的单表持久化模型。"""

from typing import Optional

from sqlalchemy import Boolean, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class PluginInstallation(Base):
    """保存单插件 membership、身份 CAS revision 和持久备份状态。"""

    id = get_id_column()
    transaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    membership_before: Mapped[bool] = mapped_column(Boolean, nullable=False)
    membership_target: Mapped[Optional[bool]] = mapped_column(Boolean)
    identity_before_revision: Mapped[Optional[int]] = mapped_column(Integer)
    identity_target_revision: Mapped[Optional[int]] = mapped_column(Integer)
    package_existed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    persistent_backup_existed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "transaction_id",
            name="uq_plugininstallation_transaction_id",
        ),
        Index("ix_plugininstallation_plugin_id", "plugin_id"),
        Index("ix_plugininstallation_phase", "phase"),
    )
