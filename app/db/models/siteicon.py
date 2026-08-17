from typing import Optional
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.decorators import db_query, async_db_query


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
    @db_query
    def get_by_domain(cls, db: Session, domain: str):
        return db.execute(select(cls).where(cls.domain == domain)).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_by_domain(cls, db: AsyncSession, domain: str):
        result = await db.execute(select(cls).where(cls.domain == domain))
        return result.scalar_one_or_none()
