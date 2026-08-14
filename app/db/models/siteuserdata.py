from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, String, Float, JSON, Index, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, mapped_column

from app.db import db_query, db_update, Base, get_id_column, async_db_query


class SiteUserData(Base):
    """
    站点数据表
    """
    id = get_id_column()
    # 站点域名
    domain = mapped_column(String)
    # 站点名称
    name = mapped_column(String)
    # 用户名
    username = mapped_column(String)
    # 用户ID
    userid = mapped_column(String)
    # 用户等级
    user_level = mapped_column(String)
    # 加入时间
    join_at = mapped_column(String)
    # 积分
    bonus = mapped_column(Float, default=0)
    # 上传量
    upload = mapped_column(Float, default=0)
    # 下载量
    download = mapped_column(Float, default=0)
    # 分享率
    ratio = mapped_column(Float, default=0)
    # 做种数
    seeding = mapped_column(Float, default=0)
    # 下载数
    leeching = mapped_column(Float, default=0)
    # 做种体积
    seeding_size = mapped_column(Float, default=0)
    # 下载体积
    leeching_size = mapped_column(Float, default=0)
    # 做种人数, 种子大小 JSON
    seeding_info = mapped_column(JSON, default=dict)
    # 未读消息
    message_unread = mapped_column(Integer, default=0)
    # 未读消息内容 JSON
    message_unread_contents = mapped_column(JSON, default=list)
    # 错误信息
    err_msg = mapped_column(String)
    # 更新日期
    updated_day = mapped_column(String, default=datetime.now().strftime('%Y-%m-%d'))
    # 更新时间
    updated_time = mapped_column(String, default=datetime.now().strftime('%H:%M:%S'))

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
        return db.execute(statement).scalars().all()

    @classmethod
    @async_db_query
    async def async_get_by_domain(cls, db: AsyncSession, domain: str, workdate: Optional[str] = None, worktime: Optional[str] = None):
        query = select(cls).filter(cls.domain == domain)
        if workdate and worktime:
            query = query.filter(cls.updated_day == workdate, cls.updated_time == worktime)
        elif workdate:
            query = query.filter(cls.updated_day == workdate)
        result = await db.execute(query)
        return result.scalars().all()

    @classmethod
    @db_query
    def get_by_date(cls, db: Session, date: str):
        return db.execute(select(cls).where(cls.updated_day == date)).scalars().all()

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
        return db.execute(
            select(cls).join(
                subquery,
                (cls.domain == subquery.c.domain) &
                (cls.updated_day == subquery.c.latest_update_day)
            ).order_by(cls.updated_time.desc())
        ).scalars().all()

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
        return result.scalars().all()

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
        return db.execute(
            delete(cls).where(cls.id.in_(ids)),
            execution_options={"synchronize_session": False},
        ).rowcount
