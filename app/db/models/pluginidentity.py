"""已安装物理插件来源身份模型。"""

from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class PluginIdentity(Base):
    """持久化一份大小写无关、可条件更新的物理插件来源身份。"""

    id = get_id_column()
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trusted_source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    trusted_source_key: Mapped[Optional[str]] = mapped_column(String(255))
    binding_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_source_key: Mapped[Optional[str]] = mapped_column(String(255))
    declared_version: Mapped[Optional[str]] = mapped_column(String(64))
    package_generation: Mapped[Optional[str]] = mapped_column(String(8))
    system_version: Mapped[Optional[str]] = mapped_column(String(128))
    supports_v3: Mapped[Optional[bool]] = mapped_column(Boolean)
    supports_v3t: Mapped[Optional[bool]] = mapped_column(Boolean)
    payload_receipt: Mapped[Optional[str]] = mapped_column(String(71))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    bound_at: Mapped[Optional[str]] = mapped_column(String(40))
    payload_applied_at: Mapped[Optional[str]] = mapped_column(String(40))

    __table_args__ = (
        UniqueConstraint(
            "normalized_plugin_id",
            name="uq_pluginidentity_normalized_plugin_id",
        ),
        CheckConstraint(
            "normalized_plugin_id <> '' "
            "AND normalized_plugin_id = lower(normalized_plugin_id)",
            name="ck_pluginidentity_normalized_plugin_id",
        ),
        CheckConstraint("revision >= 1", name="ck_pluginidentity_revision"),
    )
