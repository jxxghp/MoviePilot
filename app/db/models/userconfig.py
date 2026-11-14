"""
按用户隔离的配置键值的 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from typing import Any, Optional, Self

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column


class UserConfig(Base):
    """
    用户配置表
    """

    id = get_id_column()
    # 用户名
    username: Mapped[str] = mapped_column(
        String,
        ForeignKey(
            "user.name",
            name="fk_userconfig_username_user",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    # 配置键
    key: Mapped[str] = mapped_column(String, nullable=False)
    # 值
    value: Mapped[Optional[Any]] = mapped_column(JSON)

    __table_args__ = (
        # 用户名和配置键联合唯一
        UniqueConstraint(
            "username",
            "key",
            name="uq_userconfig_username_key",
        ),
    )

    @classmethod
    def get_by_key(cls, db: Session, username: str, key: str) -> Optional[Self]:
        """
        在调用方 Session 中查询用户配置。
        """
        config: Optional[Self] = db.execute(select(cls).where(cls.username == username, cls.key == key)).scalars().first()
        return config

    def delete_by_key(self, db: Session, username: str, key: str) -> bool:
        """
        在调用方持有的事务中暂存指定用户配置删除。
        """
        userconfig = self.get_by_key(db=db, username=username, key=key)
        if userconfig:
            db.delete(userconfig)
        return True
