from datetime import datetime

from sqlalchemy import Boolean, Column, Integer, String, JSON, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base, async_db_query, async_db_update, get_id_column


class Site(Base):
    """
    站点表
    """
    id = get_id_column()
    # 站点名
    name = Column(String, nullable=False)
    # 域名Key
    domain = Column(String, index=True)
    # 站点地址
    url = Column(String, nullable=False)
    # 站点优先级
    pri = Column(Integer, default=1)
    # RSS地址，未启用
    rss = Column(String)
    # Cookie
    cookie = Column(String)
    # User-Agent
    ua = Column(String)
    # ApiKey
    apikey = Column(String)
    # Token
    token = Column(String)
    # 是否使用代理 0-否，1-是
    proxy = Column(Integer)
    # 过滤规则
    filter = Column(String)
    # 是否渲染
    render = Column(Integer)
    # 是否公开站点
    public = Column(Integer)
    # 附加信息
    note = Column(JSON)
    # 流控单位周期
    limit_interval = Column(Integer, default=0)
    # 流控次数
    limit_count = Column(Integer, default=0)
    # 流控间隔
    limit_seconds = Column(Integer, default=0)
    # 超时时间
    timeout = Column(Integer, default=15)
    # 是否启用
    is_active = Column(Boolean(), default=True)
    # 创建时间
    lst_mod_date = Column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    # 下载器
    downloader = Column(String)

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
    @db_query
    def get_actives(cls, db: Session):
        return db.query(cls).filter(cls.is_active).all()

    @classmethod
    @async_db_query
    async def async_get_actives(cls, db: AsyncSession):
        result = await db.execute(select(cls).where(cls.is_active))
        return result.scalars().all()

    @classmethod
    @db_query
    def list_order_by_pri(cls, db: Session):
        return db.query(cls).order_by(cls.pri).all()

    @classmethod
    @async_db_query
    async def async_list_order_by_pri(cls, db: AsyncSession):
        result = await db.execute(select(cls).order_by(cls.pri))
        return result.scalars().all()

    @classmethod
    @db_query
    def get_domains_by_ids(cls, db: Session, ids: list):
        return [r[0] for r in db.query(cls.domain).filter(cls.id.in_(ids)).all()]

    @classmethod
    @db_update
    def reset(cls, db: Session):
        db.query(cls).delete()

    @classmethod
    @async_db_update
    async def async_reset(cls, db: AsyncSession):
        await db.execute(delete(cls))
