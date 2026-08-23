from typing import Optional
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.decorators import run_legacy_async_query


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
    def get_by_domain(cls, db: Session, domain: str):
        """在调用方 Session 中查询站点图标。"""
        return db.execute(select(cls).where(cls.domain == domain)).scalars().first()

    @classmethod
    async def async_get_by_domain(
        cls,
        db: AsyncSession | None = None,
        domain: str | None = None,
    ):
        """在调用方 AsyncSession 中查询站点图标。"""
        if domain is None:
            raise TypeError("domain is required")

        async def query(session: AsyncSession):
            """在给定异步会话中执行站点图标查询。"""
            result = await session.execute(select(cls).where(cls.domain == domain))
            return result.scalar_one_or_none()

        if isinstance(db, AsyncSession):
            return await query(db)
        return await run_legacy_async_query(query)
