from datetime import datetime

from sqlalchemy import Column, Integer, String, JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, db_update, get_id_column, Base, async_db_query


class SiteStatistic(Base):
    """
    站点统计表
    """
    id = get_id_column()
    # 域名Key
    domain = Column(String, index=True)
    # 成功次数
    success = Column(Integer)
    # 失败次数
    fail = Column(Integer)
    # 平均耗时 秒
    seconds = Column(Integer)
    # 最后一次访问状态 0-成功 1-失败
    lst_state = Column(Integer)
    # 最后访问时间
    lst_mod_date = Column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    # 耗时记录 Json
    note = Column(JSON)

    @classmethod
    @db_query
    def get_by_domain(cls, db: Session, domain: str):
        return db.query(cls).filter(cls.domain == domain).first()

    @classmethod
    @async_db_query
    async def async_get_by_domain(cls, db: AsyncSession, domain: str):
        result = await db.execute(select(cls).where(cls.domain == domain))
        return result.scalar_one_or_none()

    @classmethod
    @db_update
    def reset(cls, db: Session):
        db.query(cls).delete()
