from typing import List, Optional

from sqlalchemy import Column, Integer, String, JSON, Index, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base, get_id_column, async_db_query


class Message(Base):
    """
    消息表
    """
    id = get_id_column()
    # 消息渠道
    channel = Column(String)
    # 消息来源
    source = Column(String)
    # 消息类型
    mtype = Column(String)
    # 标题
    title = Column(String)
    # 文本内容
    text = Column(String)
    # 图片
    image = Column(String)
    # 链接
    link = Column(String)
    # 用户ID
    userid = Column(String)
    # 登记时间
    reg_time = Column(String)
    # 消息方向：0-接收息，1-发送消息
    action = Column(Integer)
    # 附件json
    note = Column(JSON)

    __table_args__ = (
        Index('ix_message_reg_time_id', 'reg_time', 'id'),
    )

    @db_update
    def create_and_to_dict(self, db: Session) -> dict:
        """
        创建消息记录并返回写入后的字段字典。
        """
        db.add(self)
        db.flush()
        return self.to_dict()

    @classmethod
    @db_query
    def list_by_page(cls, db: Session, page: Optional[int] = 1, count: Optional[int] = 30) -> List["Message"]:
        """
        分页获取消息记录。
        """
        return (
            db.query(cls)
            .order_by(cls.reg_time.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
            .all()
        )

    @classmethod
    @db_query
    def exists_by_source(cls, db: Session, source: str) -> bool:
        """
        判断指定来源标识的消息记录是否存在。

        :param db: 数据库会话
        :param source: 消息来源唯一标识
        :return: 是否存在匹配记录
        """
        return db.query(cls.id).filter(cls.source == source).first() is not None

    @classmethod
    @async_db_query
    async def async_list_by_page(
            cls, db: AsyncSession, page: Optional[int] = 1, count: Optional[int] = 30
    ) -> List["Message"]:
        """
        异步分页获取消息记录。
        """
        result = await db.execute(
            select(cls)
            .order_by(cls.reg_time.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        )
        return result.scalars().all()

    @classmethod
    @async_db_query
    async def async_list_sent_by_page(
            cls,
            db: AsyncSession,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
            all_clear_before: Optional[str] = None,
            system_clear_before: Optional[str] = None,
            media_clear_before: Optional[str] = None,
    ) -> List["Message"]:
        """
        分页获取系统发送的通知消息。
        """
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

        result = await db.execute(
            statement
            .order_by(cls.reg_time.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        )
        return result.scalars().all()

    @classmethod
    @db_update
    def delete_before(
        cls,
        db: Session,
        before_time: str,
        limit: Optional[int] = 500,
    ) -> int:
        """
        分批删除指定时间之前的消息记录。
        """
        ids = [
            row[0]
            for row in db.query(cls.id)
            .filter(cls.reg_time < before_time)
            .order_by(cls.id.asc())
            .limit(limit)
            .all()
        ]
        if not ids:
            return 0
        return (
            db.query(cls)
            .filter(cls.id.in_(ids))
            .delete(synchronize_session=False)
        )
