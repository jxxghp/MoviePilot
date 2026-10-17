"""
站点请求成功率与耗时统计的 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from datetime import datetime
from typing import Any, Optional, Self

from sqlalchemy import JSON, Integer, String, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column


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
    def get_by_domain(cls, db: Session, domain: str) -> Optional[Self]:
        """
        在调用方 Session 中查询站点统计。
        """
        statistic: Optional[Self] = db.execute(select(cls).where(cls.domain == domain)).scalars().first()
        return statistic

    @classmethod
    async def async_get_by_domain(
        cls,
        db: AsyncSession,
        domain: str,
    ) -> Optional[Self]:
        """
        在调用方 AsyncSession 中查询站点统计。
        """
        result = await db.execute(select(cls).where(cls.domain == domain))
        statistic: Optional[Self] = result.scalar_one_or_none()
        return statistic

    @classmethod
    def reset(cls, db: Session) -> None:
        """
        在调用方持有的事务中暂存统计表清空操作。
        """
        db.execute(delete(cls))
