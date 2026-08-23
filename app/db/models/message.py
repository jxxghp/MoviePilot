from typing import Any, List, Optional

from sqlalchemy import Integer, String, JSON, Index, and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.decorators import legacy_async_db_query, legacy_db_query


class Message(Base):
    """
    消息表
    """
    id = get_id_column()
    # 消息渠道
    channel: Mapped[Optional[str]] = mapped_column(String)
    # 消息来源
    source: Mapped[Optional[str]] = mapped_column(String)
    # 消息类型
    mtype: Mapped[Optional[str]] = mapped_column(String)
    # 标题
    title: Mapped[Optional[str]] = mapped_column(String)
    # 文本内容
    text: Mapped[Optional[str]] = mapped_column(String)
    # 图片
    image: Mapped[Optional[str]] = mapped_column(String)
    # 链接
    link: Mapped[Optional[str]] = mapped_column(String)
    # 用户ID
    userid: Mapped[Optional[str]] = mapped_column(String)
    # 登记时间
    reg_time: Mapped[Optional[str]] = mapped_column(String)
    # 消息方向：0-接收息，1-发送消息
    action: Mapped[Optional[int]] = mapped_column(Integer)
    # 附件json
    note: Mapped[Optional[Any]] = mapped_column(JSON)

    __table_args__ = (
        Index('ix_message_reg_time_id', 'reg_time', 'id'),
    )

    def create_and_to_dict(self, db: Session) -> dict:
        """
        创建消息记录并返回写入后的字段字典。
        """
        db.add(self)
        db.flush()
        return self.to_dict()

    @classmethod
    @legacy_db_query
    def list_by_page(
        cls,
        db: Session | None = None,
        page: int = 1,
        count: int = 30,
    ) -> List["Message"]:
        """
        分页获取消息记录，兼容显式会话和旧插件无会话调用。
        """
        def query(session: Session) -> List["Message"]:
            """在给定同步会话中执行消息分页查询。"""
            return list(session.execute(
                select(cls)
                .order_by(cls.reg_time.desc(), cls.id.desc())
                .offset((page - 1) * count)
                .limit(count)
            ).scalars().all())

        return query(db)

    @classmethod
    @legacy_db_query
    def exists_by_source(
        cls,
        db: Session | str | None = None,
        source: str | None = None,
    ) -> bool:
        """
        判断指定来源标识的消息记录是否存在。

        :param db: 数据库会话
        :param source: 消息来源唯一标识
        :return: 是否存在匹配记录
        """
        if source is None and isinstance(db, str):
            source, db = db, None
        if source is None:
            raise TypeError("source is required")

        def query(session: Session) -> bool:
            """在给定同步会话中执行来源存在性查询。"""
            return session.execute(
                select(cls.id).where(cls.source == source).limit(1)
            ).scalars().first() is not None

        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_list_by_page(
            cls, db: AsyncSession | None = None, page: int = 1, count: int = 30
    ) -> List["Message"]:
        """
        异步分页获取消息记录。
        """
        async def query(session: AsyncSession) -> List["Message"]:
            """在给定异步会话中执行消息分页查询。"""
            result = await session.execute(
                select(cls)
                .order_by(cls.reg_time.desc(), cls.id.desc())
                .offset((page - 1) * count)
                .limit(count)
            )
            return list(result.scalars().all())

        return await query(db)

    @classmethod
    @legacy_async_db_query
    async def async_list_sent_by_page(
            cls,
            db: AsyncSession | None = None,
            page: int = 1,
            count: int = 30,
            all_clear_before: Optional[str] = None,
            system_clear_before: Optional[str] = None,
            media_clear_before: Optional[str] = None,
    ) -> List["Message"]:
        """
        分页获取系统发送的通知消息。
        """
        async def query(session: AsyncSession) -> List["Message"]:
            """在给定异步会话中执行通知消息分页查询。"""
            statement = select(cls).where(cls.action == 1)
            if all_clear_before:
                statement = statement.where(cls.reg_time > all_clear_before)
            if system_clear_before:
                statement = statement.where(
                    or_(
                        and_(cls.image.isnot(None), cls.image != ""),
                        cls.reg_time > system_clear_before,
                    )
                )
            if media_clear_before:
                statement = statement.where(
                    or_(
                        cls.image.is_(None),
                        cls.image == "",
                        cls.reg_time > media_clear_before,
                    )
                )
            result = await session.execute(
                statement
                .order_by(cls.reg_time.desc(), cls.id.desc())
                .offset((page - 1) * count)
                .limit(count)
            )
            return list(result.scalars().all())

        return await query(db)

    @classmethod
    def delete_before(
        cls,
        db: Session,
        before_time: str,
        limit: Optional[int] = 500,
    ) -> int:
        """
        分批删除指定时间之前的消息记录。
        """
        ids = db.execute(
            select(cls.id)
            .where(cls.reg_time < before_time)
            .order_by(cls.id.asc())
            .limit(limit)
        ).scalars().all()
        if not ids:
            return 0
        return execute_dml(
            db, delete(cls).where(cls.id.in_(ids)),
            execution_options={"synchronize_session": False},
        )
