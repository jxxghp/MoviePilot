import time
from typing import Optional, Union

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, select

from app.db.base import DbOper
from app.db.models.message import Message
from app.schemas.notification import ChannelRef, channel_identity
from app.schemas.message import MessageType


class MessageOper(DbOper):
    """
    消息数据管理
    """

    def __init__(self, db: Optional[Union[Session, AsyncSession]] = None):
        super().__init__(db)

    def add(self,
            channel: Optional[ChannelRef] = None,
            source: Optional[str] = None,
            mtype: Optional[MessageType] = None,
            title: Optional[str] = None,
            text: Optional[str] = None,
            image: Optional[str] = None,
            link: Optional[str] = None,
            userid: Optional[str] = None,
            action: Optional[int] = 1,
            note: Optional[Union[list, dict]] = None,
            **kwargs) -> dict:
        """
        新增消息
        :param channel: 消息渠道
        :param source: 来源
        :param mtype: 消息类型
        :param title: 标题
        :param text: 文本内容
        :param image: 图片
        :param link: 链接
        :param userid: 用户ID
        :param action: 消息方向：0-接收息，1-发送消息
        :param note: 附件json
        """
        kwargs.update({
            "channel": channel_identity(channel) or '',
            "source": source,
            "mtype": mtype.value if mtype else '',
            "title": title,
            "text": text,
            "image": image,
            "link": link,
            "userid": userid,
            "action": action,
            "reg_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "note": note or {}
        })

        # 从kwargs中去掉Message中没有的字段
        for k in list(kwargs.keys()):
            if k not in Message.__table__.columns.keys():  # noqa
                kwargs.pop(k)

        message = Message(**kwargs)
        return self._execute_sync_write(message.create_and_to_dict)

    async def async_add(self,
                        channel: Optional[ChannelRef] = None,
                        source: Optional[str] = None,
                        mtype: Optional[MessageType] = None,
                        title: Optional[str] = None,
                        text: Optional[str] = None,
                        image: Optional[str] = None,
                        link: Optional[str] = None,
                        userid: Optional[str] = None,
                        action: Optional[int] = 1,
                        note: Optional[Union[list, dict]] = None,
                        **kwargs) -> Message:
        """
        异步新增消息
        """
        kwargs.update({
            "channel": channel_identity(channel) or '',
            "source": source,
            "mtype": mtype.value if mtype else '',
            "title": title,
            "text": text,
            "image": image,
            "link": link,
            "userid": userid,
            "action": action,
            "reg_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "note": note or {}
        })

        # 从kwargs中去掉Message中没有的字段
        for k in list(kwargs.keys()):
            if k not in Message.__table__.columns.keys():  # noqa
                kwargs.pop(k)

        return await self._stage_async_create(Message(**kwargs))

    def list_by_page(self, page: int = 1, count: int = 30) -> list[Message]:
        """
        分页获取消息记录。
        """
        return self._execute_sync_query(
            lambda session: list(session.execute(
                select(Message)
                .order_by(Message.reg_time.desc(), Message.id.desc())
                .offset((page - 1) * count)
                .limit(count)
            ).scalars().all())
        )

    def exists_by_source(self, source: str) -> bool:
        """
        判断指定来源标识的消息记录是否存在。

        :param source: 消息来源唯一标识
        :return: 是否存在匹配记录
        """
        return self._execute_sync_query(
            lambda session: session.execute(
                select(Message.id).where(Message.source == source).limit(1)
            ).scalars().first() is not None
        )

    async def async_list_by_page(
            self, page: int = 1, count: int = 30
    ) -> list[Message]:
        """
        分页获取消息记录。
        """
        async def query(session: AsyncSession) -> list[Message]:
            """在调用方异步会话中执行消息分页查询。"""
            result = await session.execute(
                select(Message)
                .order_by(Message.reg_time.desc(), Message.id.desc())
                .offset((page - 1) * count)
                .limit(count)
            )
            return list(result.scalars().all())

        return await self._execute_async_query(query)

    async def async_list_sent_by_page(
            self,
            page: int = 1,
            count: int = 30,
            all_clear_before: Optional[str] = None,
            system_clear_before: Optional[str] = None,
            media_clear_before: Optional[str] = None,
    ) -> list[Message]:
        """
        分页获取系统发送的通知消息。
        """
        async def query(session: AsyncSession) -> list[Message]:
            """在调用方异步会话中执行通知消息分页查询。"""
            statement = select(Message).where(Message.action == 1)
            if all_clear_before:
                statement = statement.where(Message.reg_time > all_clear_before)
            if system_clear_before:
                statement = statement.where(or_(
                    and_(Message.image.isnot(None), Message.image != ""),
                    Message.reg_time > system_clear_before,
                ))
            if media_clear_before:
                statement = statement.where(or_(
                    Message.image.is_(None),
                    Message.image == "",
                    Message.reg_time > media_clear_before,
                ))
            result = await session.execute(
                statement
                .order_by(Message.reg_time.desc(), Message.id.desc())
                .offset((page - 1) * count)
                .limit(count)
            )
            return list(result.scalars().all())

        return await self._execute_async_query(query)
