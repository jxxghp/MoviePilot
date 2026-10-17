"""
站点图标缓存的 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from typing import Optional, Self

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column


class SiteIcon(Base):
    """
    站点图标表
    """
    id = get_id_column()
    # 站点名称
    name: Mapped[str] = mapped_column(String, nullable=False)
    # 域名Key
    domain: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 图标地址
    url: Mapped[str] = mapped_column(String, nullable=False)
    # 图标Base64
    base64: Mapped[Optional[str]] = mapped_column(String)

    @classmethod
    def get_by_domain(cls, db: Session, domain: str) -> Optional[Self]:
        """
        在调用方 Session 中查询站点图标。
        """
        icon: Optional[Self] = db.execute(select(cls).where(cls.domain == domain)).scalars().first()
        return icon

    @classmethod
    async def async_get_by_domain(
        cls,
        db: AsyncSession,
        domain: str,
    ) -> Optional[Self]:
        """
        在调用方 AsyncSession 中查询站点图标。
        """
        result = await db.execute(select(cls).where(cls.domain == domain))
        icon: Optional[Self] = result.scalar_one_or_none()
        return icon
