"""
媒体服务器条目缓存的 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from datetime import datetime
from typing import Any, List, Optional, Self

from sqlalchemy import JSON, Index, Select, String, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.models._constraints import media_identity_constraint
from app.schemas.types import MediaSource


class MediaServerItem(Base):
    """
    媒体服务器媒体条目表
    """
    id = get_id_column()
    # 服务器类型
    server: Mapped[Optional[str]] = mapped_column(String)
    # 媒体库ID
    library: Mapped[Optional[str]] = mapped_column(String)
    # 媒体服务器分配的条目标识。
    item_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 类型
    item_type: Mapped[Optional[str]] = mapped_column(String)
    # 标题
    title: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 原标题
    original_title: Mapped[Optional[str]] = mapped_column(String)
    # 年份
    year: Mapped[Optional[str]] = mapped_column(String)
    # 媒体数据源与原生ID
    media_source: Mapped[Optional[str]] = mapped_column(String, index=True)
    media_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 路径
    path: Mapped[Optional[str]] = mapped_column(String)
    # 季集
    seasoninfo: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 备注
    note: Mapped[Optional[Any]] = mapped_column(JSON)
    # 同步时间
    lst_mod_date: Mapped[Optional[str]] = mapped_column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    __table_args__ = (
        media_identity_constraint("mediaserveritem"),
        Index('ux_mediaserveritem_server_item_id', 'server', 'item_id', unique=True),
        Index(
            'ix_mediaserveritem_media_identity_type',
            'media_source', 'media_id', 'item_type',
        ),
    )

    @classmethod
    def get_by_itemid(cls, db: Session, item_id: str) -> Optional[Self]:
        """
        按媒体条目 ID 查询首条记录。
        """
        item: Optional[Self] = db.execute(select(cls).where(cls.item_id == item_id)).scalars().first()
        return item

    @classmethod
    def get_by_server_itemid(cls, db: Session, server: str, item_id: str) -> Optional[Self]:
        """
        在指定服务器中按媒体条目 ID 查询首条记录。
        """
        item: Optional[Self] = db.execute(
            select(cls).where(cls.server == server, cls.item_id == item_id)
        ).scalars().first()
        return item

    @classmethod
    def empty(cls, db: Session, server: Optional[str] = None) -> None:
        """
        在调用方事务中暂存媒体服务器条目清空操作。
        """
        statement = delete(cls)
        if server is not None:
            statement = statement.where(cls.server == server)
        db.execute(statement, execution_options={"synchronize_session": False})

    @classmethod
    def delete_stale(cls, db: Session, server: str, sync_time: str) -> int:
        """
        在调用方事务中删除本轮同步未更新的条目。
        """
        return execute_dml(
            db,
            delete(cls).where(
                cls.server == server,
                or_(cls.lst_mod_date.is_(None), cls.lst_mod_date != sync_time),
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def delete_excluded_servers(cls, db: Session, servers: List[str]) -> int:
        """
        在调用方事务中删除不属于启用服务器的条目。
        """
        statement = delete(cls)
        if servers:
            statement = statement.where(
                or_(cls.server.is_(None), ~cls.server.in_(servers))
            )
        return execute_dml(
            db, statement, execution_options={"synchronize_session": False}
        )

    @classmethod
    def exist_by_media_identity(
            cls, db: Session, media_source: MediaSource | str, media_id: str,
            mtype: Optional[str],
            server: Optional[str] = None,
    ) -> Optional[Self]:
        """
        按规范媒体身份和类型查询媒体服务器条目。
        """
        statement = select(cls).where(
            cls.media_source == str(media_source),
            cls.media_id == str(media_id),
            cls.item_type == mtype,
        )
        if server:
            statement = statement.where(cls.server == server)
        item: Optional[Self] = db.execute(statement).scalars().first()
        return item

    @classmethod
    def exists_by_title(
            cls, db: Session, title: str, mtype: Optional[str], year: Optional[str | int],
            server: Optional[str] = None,
    ) -> Optional[Self]:
        """
        按标题及可选类型、年份和服务器查询首条记录。
        """
        statement = select(cls).where(cls.title == title)
        if mtype:
            statement = statement.where(cls.item_type == mtype)
        if year:
            statement = statement.where(cls.year == str(year))
        if server:
            statement = statement.where(cls.server == server)
        item: Optional[Self] = db.execute(statement).scalars().first()
        return item

    @classmethod
    def _cross_source_title_statement(
            cls, title: str, mtype: Optional[str], year: str | int,
            media_source: MediaSource | str, server: Optional[str] = None,
    ) -> Select[tuple["MediaServerItem"]]:
        """
        构造跨来源标题回退查询；同来源条目在精确身份未命中时必然是另一部作品。
        """
        statement = select(cls).where(
            cls.title == title,
            cls.year == str(year),
            or_(cls.media_source.is_(None), cls.media_source != str(media_source)),
        )
        if mtype:
            statement = statement.where(cls.item_type == mtype)
        if server:
            statement = statement.where(cls.server == server)
        return statement.order_by(cls.id)

    @classmethod
    def list_cross_source_by_title(
            cls, db: Session, title: str, mtype: Optional[str], year: str | int,
            media_source: MediaSource | str, server: Optional[str] = None,
    ) -> List["MediaServerItem"]:
        """
        返回可作为跨来源候选的同标题同年份条目。
        """
        statement = cls._cross_source_title_statement(
            title, mtype, year, media_source, server
        )
        return list(db.execute(statement).scalars().all())

    @classmethod
    async def async_get_by_itemid(cls, db: AsyncSession, item_id: str) -> Optional[Self]:
        """
        异步按媒体条目 ID 查询首条记录。
        """
        result = await db.execute(select(cls).filter(cls.item_id == item_id))
        item: Optional[Self] = result.scalars().first()
        return item

    @classmethod
    async def async_exist_by_media_identity(
            cls, db: AsyncSession, media_source: MediaSource | str, media_id: str,
            mtype: Optional[str],
            server: Optional[str] = None,
    ) -> Optional[Self]:
        """
        异步按规范媒体身份和类型查询媒体服务器条目。
        """
        statement = select(cls).filter(
            cls.media_source == str(media_source),
            cls.media_id == str(media_id),
            cls.item_type == mtype,
        )
        if server:
            statement = statement.filter(cls.server == server)
        result = await db.execute(statement)
        item: Optional[Self] = result.scalars().first()
        return item

    @classmethod
    async def async_list_cross_source_by_title(
            cls, db: AsyncSession, title: str, mtype: Optional[str], year: str | int,
            media_source: MediaSource | str, server: Optional[str] = None,
    ) -> List["MediaServerItem"]:
        """
        异步返回可作为跨来源候选的同标题同年份条目。
        """
        statement = cls._cross_source_title_statement(
            title, mtype, year, media_source, server
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    @classmethod
    async def async_exists_by_title(
            cls, db: AsyncSession, title: str, mtype: Optional[str], year: Optional[str | int],
            server: Optional[str] = None,
    ) -> Optional[Self]:
        """
        异步按标题及可选类型、年份和服务器查询首条记录。
        """
        if not mtype and not year:
            statement = select(cls).filter(cls.title == title)
        elif not year:
            statement = select(cls).filter(cls.title == title, cls.item_type == mtype)
        elif not mtype:
            statement = select(cls).filter(cls.title == title, cls.year == str(year))
        else:
            statement = select(cls).filter(
                cls.title == title,
                cls.item_type == mtype,
                cls.year == str(year),
            )
        if server:
            statement = statement.filter(cls.server == server)
        result = await db.execute(statement)
        item: Optional[Self] = result.scalars().first()
        return item
