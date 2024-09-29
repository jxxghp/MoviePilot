from datetime import datetime

from sqlalchemy import Column, Integer, String, Sequence, Float, JSON
from sqlalchemy.orm import Session

from app.db import db_query, Base


class SiteUserData(Base):
    """
    站点数据表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 站点域名
    domain = Column(String, index=True)
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
    seeding_info = Column(JSON)
    # 未读消息
    message_unread = Column(Integer, default=0)
    # 未读消息内容 JSON
    message_unread_contents = Column(JSON)
    # 错误信息
    err_msg = Column(String)
    # 更新日期
    updated_day = Column(String, index=True, default=datetime.now().strftime('%Y-%m-%d'))
    # 更新时间
    updated_time = Column(String, default=datetime.now().strftime('%H:%M:%S'))

    @staticmethod
    @db_query
    def get_by_domain(db: Session, domain: str, workdate: str = None, worktime: str = None):
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
