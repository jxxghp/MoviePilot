from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, Float, JSON, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, Base, get_id_column, async_db_query


class SiteUserData(Base):
    """
    站点数据表
    """
    id = get_id_column()
    # 站点域名
    domain = Column(String, index=True)
    # 站点名称
    name = Column(String)
    # 用户名
    username = Column(String)
    # 用户ID
    userid = Column(String)
    # 用户等级
    user_level = Column(String)
    # 加入时间
    join_at = Column(String)
    # 积分
    bonus = Column(Float, default=0)
    # 上传量
    upload = Column(Float, default=0)
    # 下载量
    download = Column(Float, default=0)
    # 分享率
    ratio = Column(Float, default=0)
    # 做种数
    seeding = Column(Float, default=0)
    # 下载数
    leeching = Column(Float, default=0)
    # 做种体积
    seeding_size = Column(Float, default=0)
    # 下载体积
    leeching_size = Column(Float, default=0)
    # 做种人数, 种子大小 JSON
    seeding_info = Column(JSON, default=dict)
    # 未读消息
    message_unread = Column(Integer, default=0)
    # 未读消息内容 JSON
    message_unread_contents = Column(JSON, default=list)
    # 错误信息
    err_msg = Column(String)
    # 更新日期
    updated_day = Column(String, index=True, default=datetime.now().strftime('%Y-%m-%d'))
    # 更新时间
    updated_time = Column(String, default=datetime.now().strftime('%H:%M:%S'))

    @classmethod
    @db_query
    def get_by_domain(cls, db: Session, domain: str, workdate: Optional[str] = None, worktime: Optional[str] = None):
        if workdate and worktime:
            return db.query(cls).filter(cls.domain == domain,
                                        cls.updated_day == workdate,
                                        cls.updated_time == worktime).all()
        elif workdate:
            return db.query(cls).filter(cls.domain == domain,
                                        cls.updated_day == workdate).all()
        return db.query(cls).filter(cls.domain == domain).all()

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
        return db.query(cls).filter(cls.updated_day == date).all()

    @classmethod
    @db_query
    def get_latest(cls, db: Session):
        """
        获取各站点最新一天的数据
        """
        subquery = (
            db.query(
                cls.domain,
                func.max(cls.updated_day).label('latest_update_day')
            )
            .group_by(cls.domain)
            .filter(or_(cls.err_msg.is_(None), cls.err_msg == ""))
            .subquery()
        )

        # 主查询：按 domain 和 updated_day 获取最新的记录
        return db.query(cls).join(
            subquery,
            (cls.domain == subquery.c.domain) &
            (cls.updated_day == subquery.c.latest_update_day)
        ).order_by(cls.updated_time.desc()).all()

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
