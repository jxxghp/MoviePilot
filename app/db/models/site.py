from typing import Any, Optional
from datetime import datetime

from sqlalchemy import Boolean, Integer, String, JSON, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.decorators import db_query, async_db_query


class Site(Base):
    """
    站点表
    """
    id = get_id_column()
    # 站点名
    name: Mapped[str] = mapped_column(String, nullable=False)
    # 域名Key
    domain: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 站点地址
    url: Mapped[str] = mapped_column(String, nullable=False)
    # 站点优先级
    pri: Mapped[Optional[int]] = mapped_column(Integer, default=1)
    # RSS地址，未启用
    rss: Mapped[Optional[str]] = mapped_column(String)
    # Cookie
    cookie: Mapped[Optional[str]] = mapped_column(String)
    # User-Agent
    ua: Mapped[Optional[str]] = mapped_column(String)
    # ApiKey
    apikey: Mapped[Optional[str]] = mapped_column(String)
    # Token
    token: Mapped[Optional[str]] = mapped_column(String)
    # 是否使用代理 0-否，1-是
    proxy: Mapped[Optional[int]] = mapped_column(Integer)
    # 过滤规则
    filter: Mapped[Optional[str]] = mapped_column(String)
    # 是否渲染
    render: Mapped[Optional[int]] = mapped_column(Integer)
    # 是否公开站点
    public: Mapped[Optional[int]] = mapped_column(Integer)
    # 附加信息
    note: Mapped[Optional[Any]] = mapped_column(JSON)
    # 流控单位周期
    limit_interval: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 流控次数
    limit_count: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 流控间隔
    limit_seconds: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 超时时间
    timeout: Mapped[Optional[int]] = mapped_column(Integer, default=15)
    # 是否启用
    is_active: Mapped[Optional[bool]] = mapped_column(Boolean(), default=True)
    # 创建时间
    lst_mod_date: Mapped[Optional[str]] = mapped_column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    # 下载器
    downloader: Mapped[Optional[str]] = mapped_column(String)

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
    @async_db_query
    async def async_get_by_name(cls, db: AsyncSession, name: str):
        result = await db.execute(select(cls).where(cls.name == name))
        return result.scalar_one_or_none()

    @classmethod
    @db_query
    def get_actives(cls, db: Session):
        return list(db.execute(select(cls).where(cls.is_active.is_(True))).scalars().all())

    @classmethod
    @async_db_query
    async def async_get_actives(cls, db: AsyncSession):
        result = await db.execute(select(cls).where(cls.is_active.is_(True)))
        return list(result.scalars().all())

    @classmethod
    @db_query
    def list_order_by_pri(cls, db: Session):
        return list(db.execute(select(cls).order_by(cls.pri)).scalars().all())

    @classmethod
    @async_db_query
    async def async_list_order_by_pri(cls, db: AsyncSession):
        result = await db.execute(select(cls).order_by(cls.pri))
        return list(result.scalars().all())

    @classmethod
    @db_query
    def get_domains_by_ids(cls, db: Session, ids: list):
        return list(db.execute(select(cls.domain).where(cls.id.in_(ids))).scalars().all())

    @classmethod
    def reset(cls, db: Session):
        """在调用方持有的同步事务中暂存清空操作。"""
        db.execute(delete(cls))

    @classmethod
    async def async_reset(cls, db: AsyncSession):
        """在调用方持有的异步事务中暂存清空操作。"""
        await db.execute(delete(cls))
