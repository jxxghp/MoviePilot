from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, Sequence, Float, JSON, func, or_
from sqlalchemy.orm import Session

from app.db import db_query, Base


class SiteUserData(Base):
    """
    站点数据表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 站点域名
    domain = Column(String, index=True)
    # 站点名称
    name = Column(String)
    # 用户名
    username = Column(String)
    # 用户ID
    userid = Column(Integer)
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

    @staticmethod
    @db_query
    def get_by_domain(db: Session, domain: str, workdate: Optional[str] = None, worktime: Optional[str] = None):
        if workdate and worktime:
            return db.query(SiteUserData).filter(SiteUserData.domain == domain,
                                                 SiteUserData.updated_day == workdate,
                                                 SiteUserData.updated_time == worktime).all()
        elif workdate:
            return db.query(SiteUserData).filter(SiteUserData.domain == domain,
                                                 SiteUserData.updated_day == workdate).all()
        return db.query(SiteUserData).filter(SiteUserData.domain == domain).all()

    @staticmethod
    @db_query
    def get_by_date(db: Session, date: str):
        return db.query(SiteUserData).filter(SiteUserData.updated_day == date).all()

    @staticmethod
    @db_query
    def get_latest(db: Session):
        """
        获取各站点最新一天的数据
        """
        subquery = (
            db.query(
                SiteUserData.domain,
                func.max(SiteUserData.updated_day).label('latest_update_day')
            )
            .group_by(SiteUserData.domain)
            .filter(or_(SiteUserData.err_msg.is_(None), SiteUserData.err_msg == ""))
            .subquery()
        )

        # 主查询：按 domain 和 updated_day 获取最新的记录
        return db.query(SiteUserData).join(
            subquery,
            (SiteUserData.domain == subquery.c.domain) &
            (SiteUserData.updated_day == subquery.c.latest_update_day)
        ).order_by(SiteUserData.updated_time.desc()).all()
