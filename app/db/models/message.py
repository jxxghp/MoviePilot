"""
已发送通知与消息历史的 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from typing import Any, List, Optional, Sequence

from sqlalchemy import JSON, Index, Integer, String, and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column


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
    # 独立媒体海报，历史记录不从背景图猜测
    poster: Mapped[Optional[str]] = mapped_column(String)
    # 链接
    link: Mapped[Optional[str]] = mapped_column(String)
    # 用户ID
    userid: Mapped[Optional[str]] = mapped_column(String)
    # 登记时间
    reg_time: Mapped[Optional[str]] = mapped_column(String)
    # 消息方向：0-接收消息，1-发送消息
    action: Mapped[Optional[int]] = mapped_column(Integer)
    # 附件json
    note: Mapped[Optional[Any]] = mapped_column(JSON)

    __table_args__ = (
        Index('ix_message_reg_time_id', 'reg_time', 'id'),
    )

    def create_and_to_dict(self, db: Session) -> dict[str, object]:
        """
        创建消息记录并返回写入后的字段字典。
        """
        db.add(self)
        db.flush()
        return self.to_dict()

    @classmethod
    def list_by_page(
        cls,
        db: Session,
        page: int = 1,
        count: int = 30,
    ) -> List["Message"]:
        """
        在调用方同步会话中分页获取消息记录。
        """
        return list(db.execute(
            select(cls)
            .order_by(cls.reg_time.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        ).scalars().all())

    @classmethod
    def exists_by_source(
        cls,
        db: Session,
        source: str,
    ) -> bool:
        """
        判断指定来源标识的消息记录是否存在。

        :param db: 数据库会话
        :param source: 消息来源唯一标识
        :return: 是否存在匹配记录
        """
        return db.execute(
            select(cls.id).where(cls.source == source).limit(1)
        ).scalars().first() is not None

    @classmethod
    def _visible_clause(
            cls,
            visible_mtypes: Optional[Sequence[str]] = None,
            visible_userid: Optional[str] = None,
    ) -> Optional[Any]:
        """
        构造读取侧可见性条件：广播场景的通知或本人产生的记录。

        :param visible_mtypes: 允许普通用户查看的通知场景值，None 表示不限制
        :param visible_userid: 允许查看本人记录的用户标识
        :return: 查询条件，None 表示不限制可见范围
        """
        if visible_mtypes is None:
            return None
        conditions = [cls.mtype.in_(visible_mtypes)]
        if visible_userid:
            conditions.append(cls.userid == visible_userid)
        return or_(*conditions)

    @classmethod
    async def async_list_by_page(
            cls,
            db: AsyncSession,
            page: int = 1,
            count: int = 30,
            visible_mtypes: Optional[Sequence[str]] = None,
            visible_userid: Optional[str] = None,
    ) -> List["Message"]:
        """
        异步分页获取消息记录。

        :param db: 数据库会话
        :param page: 页码，从 1 开始
        :param count: 每页最多返回的消息数
        :param visible_mtypes: 允许普通用户查看的通知场景值，None 表示不限制
        :param visible_userid: 允许查看本人记录的用户标识
        """
        statement = select(cls)
        visible = cls._visible_clause(visible_mtypes, visible_userid)
        if visible is not None:
            statement = statement.where(visible)
        result = await db.execute(
            statement
            .order_by(cls.reg_time.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        )
        return list(result.scalars().all())

    @classmethod
    async def async_list_sent_by_page(
            cls,
            db: AsyncSession,
            page: int = 1,
            count: int = 30,
            all_clear_before: Optional[str] = None,
            system_clear_before: Optional[str] = None,
            media_clear_before: Optional[str] = None,
            visible_mtypes: Optional[Sequence[str]] = None,
            visible_userid: Optional[str] = None,
    ) -> List["Message"]:
        """
        分页获取系统发送的通知消息。

        :param db: 数据库会话
        :param page: 页码，从 1 开始
        :param count: 每页最多返回的消息数
        :param all_clear_before: 全部通知的清理水位
        :param system_clear_before: 无图片系统通知的清理水位
        :param media_clear_before: 含图片媒体通知的清理水位
        :param visible_mtypes: 允许普通用户查看的通知场景值，None 表示不限制
        :param visible_userid: 允许查看本人记录的用户标识
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
        visible = cls._visible_clause(visible_mtypes, visible_userid)
        if visible is not None:
            statement = statement.where(visible)
        result = await db.execute(
            statement
            .order_by(cls.reg_time.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        )
        return list(result.scalars().all())

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
