from datetime import datetime

from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base


class SiteStatistic(Base):
    """
    站点统计表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
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
    note = Column(String)

    @staticmethod
    @db_query
    def get_by_domain(db: Session, domain: str):
        return db.query(SiteStatistic).filter(SiteStatistic.domain == domain).first()

    @staticmethod
    @db_update
    def reset(db: Session):
        db.query(SiteStatistic).delete()
