import time
from typing import Optional, Union

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import DbOper
from app.db.models.message import Message
from app.schemas import MessageChannel, NotificationType


class MessageOper(DbOper):
    """
    消息数据管理
    """

    def __init__(self, db: Union[Session, AsyncSession] = None):
        super().__init__(db)

    def add(self,
            channel: MessageChannel = None,
            source: Optional[str] = None,
            mtype: NotificationType = None,
            title: Optional[str] = None,
            text: Optional[str] = None,
            image: Optional[str] = None,
            link: Optional[str] = None,
            userid: Optional[str] = None,
            action: Optional[int] = 1,
            note: Union[list, dict] = None,
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

        return Message(**kwargs).create_and_to_dict(self._db)

    async def async_add(self,
                        channel: MessageChannel = None,
                        source: Optional[str] = None,
                        mtype: NotificationType = None,
                        title: Optional[str] = None,
                        text: Optional[str] = None,
                        image: Optional[str] = None,
                        link: Optional[str] = None,
                        userid: Optional[str] = None,
                        action: Optional[int] = 1,
                        note: Union[list, dict] = None,
                        **kwargs) -> Message:
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

        return await Message(**kwargs).async_create(self._db)

    def list_by_page(self, page: Optional[int] = 1, count: Optional[int] = 30) -> list[Message]:
        """
        分页获取消息记录。
        """
        return Message.list_by_page(self._db, page, count)

    def exists_by_source(self, source: str) -> bool:
        """
        判断指定来源标识的消息记录是否存在。

        :param source: 消息来源唯一标识
        :return: 是否存在匹配记录
        """
        return Message.exists_by_source(self._db, source)

    async def async_list_by_page(
            self, page: Optional[int] = 1, count: Optional[int] = 30
    ) -> list[Message]:
        """
        分页获取消息记录。
        """
        return await Message.async_list_by_page(self._db, page, count)

    async def async_list_sent_by_page(
            self,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
            all_clear_before: Optional[str] = None,
            system_clear_before: Optional[str] = None,
            media_clear_before: Optional[str] = None,
    ) -> list[Message]:
        """
        分页获取系统发送的通知消息。
        """
        return await Message.async_list_sent_by_page(
            self._db,
            page,
            count,
            all_clear_before=all_clear_before,
            system_clear_before=system_clear_before,
            media_clear_before=media_clear_before,
        )
