from typing import Any, Optional
from datetime import datetime

from sqlalchemy import Integer, String, JSON, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import get_id_column, Base
from app.db.decorators import db_query, db_update, async_db_query


class SiteStatistic(Base):
    """
    站点统计表
    """
    id = get_id_column()
    # 域名Key
    domain: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 成功次数
    success: Mapped[Optional[int]] = mapped_column(Integer)
    # 失败次数
    fail: Mapped[Optional[int]] = mapped_column(Integer)
    # 平均耗时 秒
    seconds: Mapped[Optional[int]] = mapped_column(Integer)
    # 最后一次访问状态 0-成功 1-失败
    lst_state: Mapped[Optional[int]] = mapped_column(Integer)
    # 最后访问时间
    lst_mod_date: Mapped[Optional[str]] = mapped_column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    # 耗时记录 Json
    note: Mapped[Optional[Any]] = mapped_column(JSON)

    @classmethod
    @db_query
    def get_by_domain(cls, db: Session, domain: str):
        return db.execute(select(cls).where(cls.domain == domain)).scalars().first()

    @classmethod
    @async_db_query
    async def async_get_by_domain(cls, db: AsyncSession, domain: str):
        result = await db.execute(select(cls).where(cls.domain == domain))
        return result.scalar_one_or_none()

    @classmethod
    @db_update
    def reset(cls, db: Session):
        db.execute(delete(cls))
