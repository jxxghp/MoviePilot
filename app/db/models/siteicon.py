from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db import db_query, Base


class SiteIcon(Base):
    """
    站点图标表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 站点名称
    name = Column(String, nullable=False)
    # 域名Key
    domain = Column(String, index=True)
    # 图标地址
    url = Column(String, nullable=False)
    # 图标Base64
    base64 = Column(String)

    @staticmethod
    @db_query
    def get_by_domain(db: Session, domain: str):
        return db.query(SiteIcon).filter(SiteIcon.domain == domain).first()
