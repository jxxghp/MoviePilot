from typing import Any, Optional
from datetime import datetime

from sqlalchemy import Boolean, Integer, String, JSON, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.decorators import run_legacy_async_query, run_legacy_sync_query


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
    def get_by_domain(cls, db: Session | str | None = None, domain: str | None = None):
        """按域名查询站点，兼容显式会话和旧插件无会话调用。"""
        if domain is None and isinstance(db, str):
            domain, db = db, None
        if domain is None:
            raise TypeError("domain is required")

        def query(session: Session):
            """在给定同步会话中执行域名查询。"""
            return session.execute(select(cls).where(cls.domain == domain)).scalars().first()

        return query(db) if isinstance(db, Session) else run_legacy_sync_query(query)

    @classmethod
    async def async_get_by_domain(
        cls,
        db: AsyncSession | str | None = None,
        domain: str | None = None,
    ):
        """异步按域名查询站点，兼容显式会话和旧插件无会话调用。"""
        if domain is None and isinstance(db, str):
            domain, db = db, None
        if domain is None:
            raise TypeError("domain is required")

        async def query(session: AsyncSession):
            """在给定异步会话中执行域名查询。"""
            result = await session.execute(select(cls).where(cls.domain == domain))
            return result.scalar_one_or_none()

        return await query(db) if isinstance(db, AsyncSession) else await run_legacy_async_query(query)

    @classmethod
    async def async_get_by_name(
        cls,
        db: AsyncSession | str | None = None,
        name: str | None = None,
    ):
        """异步按站点名称查询，兼容显式会话和旧插件无会话调用。"""
        if name is None and isinstance(db, str):
            name, db = db, None
        if name is None:
            raise TypeError("name is required")

        async def query(session: AsyncSession):
            """在给定异步会话中执行名称查询。"""
            result = await session.execute(select(cls).where(cls.name == name))
            return result.scalar_one_or_none()

        return await query(db) if isinstance(db, AsyncSession) else await run_legacy_async_query(query)

    @classmethod
    def get_actives(cls, db: Session | None = None):
        """查询启用站点，兼容显式会话和旧插件无会话调用。"""
        def query(session: Session):
            """在给定同步会话中执行启用站点查询。"""
            return list(session.execute(select(cls).where(cls.is_active.is_(True))).scalars().all())

        return query(db) if isinstance(db, Session) else run_legacy_sync_query(query)

    @classmethod
    async def async_get_actives(cls, db: AsyncSession | None = None):
        """异步查询启用站点，兼容显式会话和旧插件无会话调用。"""
        async def query(session: AsyncSession):
            """在给定异步会话中执行启用站点查询。"""
            result = await session.execute(select(cls).where(cls.is_active.is_(True)))
            return list(result.scalars().all())

        return await query(db) if isinstance(db, AsyncSession) else await run_legacy_async_query(query)

    @classmethod
    def list_order_by_pri(cls, db: Session | None = None):
        """按优先级升序查询站点，兼容显式会话和旧插件无会话调用。"""
        def query(session: Session):
            """在给定同步会话中执行优先级查询。"""
            return list(session.execute(select(cls).order_by(cls.pri)).scalars().all())

        return query(db) if isinstance(db, Session) else run_legacy_sync_query(query)

    @classmethod
    async def async_list_order_by_pri(cls, db: AsyncSession | None = None):
        """异步按优先级升序查询站点，兼容显式会话和旧插件无会话调用。"""
        async def query(session: AsyncSession):
            """在给定异步会话中执行优先级查询。"""
            result = await session.execute(select(cls).order_by(cls.pri))
            return list(result.scalars().all())

        return await query(db) if isinstance(db, AsyncSession) else await run_legacy_async_query(query)

    @classmethod
    def get_domains_by_ids(
        cls,
        db: Session | list[int] | None = None,
        ids: list[int] | None = None,
    ):
        """按 ID 查询域名，兼容显式会话和旧插件无会话调用。"""
        if ids is None and isinstance(db, list):
            ids, db = db, None
        if ids is None:
            raise TypeError("ids is required")
        if not ids:
            return []

        def query(session: Session):
            """在给定同步会话中执行域名投影查询。"""
            return list(session.execute(select(cls.domain).where(cls.id.in_(ids))).scalars().all())

        return query(db) if isinstance(db, Session) else run_legacy_sync_query(query)

    @classmethod
    def reset(cls, db: Session):
        """在调用方持有的同步事务中暂存清空操作。"""
        db.execute(delete(cls))

    @classmethod
    async def async_reset(cls, db: AsyncSession):
        """在调用方持有的异步事务中暂存清空操作。"""
        await db.execute(delete(cls))
