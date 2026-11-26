"""
已发送通知与消息历史的数据访问入口，适配调用方会话及兼容事务执行器。
"""

import time
from typing import Any, Optional, Sequence, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.message import Message
from app.schemas.types import MessageType, NotificationChannel


class MessageOper(DbOper):
    """
    消息数据管理
    """

    def __init__(self, db: Optional[Union[Session, AsyncSession]] = None):
        super().__init__(db)

    def add(self,
            channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None,
            mtype: Optional[MessageType] = None,
            title: Optional[str] = None,
            text: Optional[str] = None,
            image: Optional[str] = None,
            link: Optional[str] = None,
            userid: Optional[str] = None,
            action: Optional[int] = 1,
            note: Optional[Union[list[Any], dict[str, Any]]] = None,
            **kwargs: Any) -> dict[str, Any]:
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
        :param action: 消息方向：0-接收消息，1-发送消息
        :param note: 附件json
        """
        kwargs.update({
            "channel": channel.value if channel else '',
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
        result: dict[str, Any] = self._execute_sync_write(message.create_and_to_dict)
        return result

    async def async_add(self,
                        channel: Optional[NotificationChannel] = None,
                        source: Optional[str] = None,
                        mtype: Optional[MessageType] = None,
                        title: Optional[str] = None,
                        text: Optional[str] = None,
                        image: Optional[str] = None,
                        link: Optional[str] = None,
                        userid: Optional[str] = None,
                        action: Optional[int] = 1,
                        note: Optional[Union[list[Any], dict[str, Any]]] = None,
                        **kwargs: Any) -> Message:
        """
        异步新增消息
        """
        kwargs.update({
            "channel": channel.value if channel else '',
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
        messages: list[Message] = self._execute_sync_query(
            lambda session: list(session.execute(
                select(Message)
                .order_by(Message.reg_time.desc(), Message.id.desc())
                .offset((page - 1) * count)
                .limit(count)
            ).scalars().all())
        )
        return messages

    def exists_by_source(self, source: str) -> bool:
        """
        判断指定来源标识的消息记录是否存在。

        :param source: 消息来源唯一标识
        :return: 是否存在匹配记录
        """
        exists: bool = self._execute_sync_query(
            lambda session: session.execute(
                select(Message.id).where(Message.source == source).limit(1)
            ).scalars().first() is not None
        )
        return exists

    async def async_list_by_page(
            self,
            page: int = 1,
            count: int = 30,
            visible_mtypes: Optional[Sequence[str]] = None,
            visible_userid: Optional[str] = None,
    ) -> list[Message]:
        """
        异步分页获取全部消息记录，不区分渠道与收发方向。

        :param page: 页码，从 1 开始
        :param count: 每页最多返回的消息数，默认 30
        :param visible_mtypes: 允许普通用户查看的通知场景值，None 表示不限制
        :param visible_userid: 允许查看本人记录的用户标识
        :return: 按登记时间和主键倒序排列的消息记录列表
        """
        async def query(session: AsyncSession) -> list[Message]:
            """
            在调用方异步会话中执行消息分页查询。
            """
            return await Message.async_list_by_page(
                session,
                page=page,
                count=count,
                visible_mtypes=visible_mtypes,
                visible_userid=visible_userid,
            )

        messages: list[Message] = await self._execute_async_query(query)
        return messages

    async def async_list_sent_by_page(
            self,
            page: int = 1,
            count: int = 30,
            all_clear_before: Optional[str] = None,
            system_clear_before: Optional[str] = None,
            media_clear_before: Optional[str] = None,
            visible_mtypes: Optional[Sequence[str]] = None,
            visible_userid: Optional[str] = None,
    ) -> list[Message]:
        """
        分页获取系统发送的通知消息。

        :param page: 页码，从 1 开始
        :param count: 每页最多返回的消息数，默认 30
        :param all_clear_before: 全部通知的清理水位
        :param system_clear_before: 无图片系统通知的清理水位
        :param media_clear_before: 含图片媒体通知的清理水位
        :param visible_mtypes: 允许普通用户查看的通知场景值，None 表示不限制
        :param visible_userid: 允许查看本人记录的用户标识
        """
        async def query(session: AsyncSession) -> list[Message]:
            """
            在调用方异步会话中执行通知消息分页查询。
            """
            return await Message.async_list_sent_by_page(
                session,
                page=page,
                count=count,
                all_clear_before=all_clear_before,
                system_clear_before=system_clear_before,
                media_clear_before=media_clear_before,
                visible_mtypes=visible_mtypes,
                visible_userid=visible_userid,
            )

        messages: list[Message] = await self._execute_async_query(query)
        return messages
