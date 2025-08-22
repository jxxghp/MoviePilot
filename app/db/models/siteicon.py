from sqlalchemy import Column, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, Base, get_id_column, async_db_query


class SiteIcon(Base):
    """
    站点图标表
    """
    id = get_id_column()
    # 站点名称
    name = Column(String, nullable=False)
    # 域名Key
    domain = Column(String, index=True)
    # 图标地址
    url = Column(String, nullable=False)
    # 图标Base64
    base64 = Column(String)

    @classmethod
    @db_query
    def get_by_domain(cls, db: Session, domain: str):
        return db.query(cls).filter(cls.domain == domain).first()

    @classmethod
    @async_db_query
    async def async_get_by_domain(cls, db: AsyncSession, domain: str):
        result = await db.execute(select(cls).where(cls.domain == domain))
        return result.scalar_one_or_none()
