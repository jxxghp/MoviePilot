from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Integer, String, Float, JSON, Index, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.decorators import async_db_query, db_query, db_update


class SiteUserData(Base):
    """
    站点数据表
    """
    id = get_id_column()
    # 站点域名
    domain: Mapped[Optional[str]] = mapped_column(String)
    # 站点名称
    name: Mapped[Optional[str]] = mapped_column(String)
    # 用户名
    username: Mapped[Optional[str]] = mapped_column(String)
    # 用户ID
    userid: Mapped[Optional[str]] = mapped_column(String)
    # 用户等级
    user_level: Mapped[Optional[str]] = mapped_column(String)
    # 加入时间
    join_at: Mapped[Optional[str]] = mapped_column(String)
    # 积分
    bonus: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 上传量
    upload: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 下载量
    download: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 分享率
    ratio: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 做种数
    seeding: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 下载数
    leeching: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 做种体积
    seeding_size: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 下载体积
    leeching_size: Mapped[Optional[float]] = mapped_column(Float, default=0)
    # 做种人数, 种子大小 JSON
    seeding_info: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 未读消息
    message_unread: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 未读消息内容 JSON
    message_unread_contents: Mapped[Optional[Any]] = mapped_column(JSON, default=list)
    # 错误信息
    err_msg: Mapped[Optional[str]] = mapped_column(String)
    # 更新日期
    updated_day: Mapped[Optional[str]] = mapped_column(String, default=datetime.now().strftime('%Y-%m-%d'))
    # 更新时间
    updated_time: Mapped[Optional[str]] = mapped_column(String, default=datetime.now().strftime('%H:%M:%S'))

    __table_args__ = (
        Index('ix_siteuserdata_updated_day_id', 'updated_day', 'id'),
        Index('ix_siteuserdata_domain_updated_day_updated_time', 'domain', 'updated_day', 'updated_time'),
    )

    @classmethod
    @db_query
    def get_by_domain(cls, db: Session, domain: str, workdate: Optional[str] = None, worktime: Optional[str] = None):
        statement = select(cls).where(cls.domain == domain)
        if workdate and worktime:
            statement = statement.where(cls.updated_day == workdate,
                                        cls.updated_time == worktime)
        elif workdate:
            statement = statement.where(cls.updated_day == workdate)
        return list(db.execute(statement).scalars().all())

    @classmethod
    @async_db_query
    async def async_get_by_domain(cls, db: AsyncSession, domain: str, workdate: Optional[str] = None, worktime: Optional[str] = None):
        query = select(cls).filter(cls.domain == domain)
        if workdate and worktime:
            query = query.filter(cls.updated_day == workdate, cls.updated_time == worktime)
        elif workdate:
            query = query.filter(cls.updated_day == workdate)
        result = await db.execute(query)
        return list(result.scalars().all())

    @classmethod
    @db_query
    def get_by_date(cls, db: Session, date: str):
        return list(db.execute(select(cls).where(cls.updated_day == date)).scalars().all())

    @classmethod
    @db_query
    def get_latest(cls, db: Session):
        """
        获取各站点最新一天的数据
        """
        subquery = (
            select(
                cls.domain,
                func.max(cls.updated_day).label('latest_update_day')
            )
            .where(or_(cls.err_msg.is_(None), cls.err_msg == ""))
            .group_by(cls.domain)
            .subquery()
        )

        # 主查询：按 domain 和 updated_day 获取最新的记录
        return list(db.execute(
            select(cls).join(
                subquery,
                (cls.domain == subquery.c.domain) &
                (cls.updated_day == subquery.c.latest_update_day)
            ).order_by(cls.updated_time.desc())
        ).scalars().all())

    @classmethod
    @async_db_query
    async def async_get_latest(cls, db: AsyncSession):
        """
        异步获取各站点最新一天的数据
        """
        subquery = (
            select(
                cls.domain,
                func.max(cls.updated_day).label('latest_update_day')
            )
            .group_by(cls.domain)
            .filter(or_(cls.err_msg.is_(None), cls.err_msg == ""))
            .subquery()
        )

        # 主查询：按 domain 和 updated_day 获取最新的记录
        result = await db.execute(
            select(cls).join(
                subquery,
                (cls.domain == subquery.c.domain) &
                (cls.updated_day == subquery.c.latest_update_day)
            ).order_by(cls.updated_time.desc()))
        return list(result.scalars().all())

    @classmethod
    @db_update
    def delete_before(
        cls,
        db: Session,
        before_day: str,
        limit: Optional[int] = 500,
    ) -> int:
        """
        分批删除指定日期之前的站点用户快照。
        """
        ids = db.execute(
            select(cls.id)
            .where(cls.updated_day < before_day)
            .order_by(cls.id.asc())
            .limit(limit)
        ).scalars().all()
        if not ids:
            return 0
        return execute_dml(
            db, delete(cls).where(cls.id.in_(ids)),
            execution_options={"synchronize_session": False},
        )
