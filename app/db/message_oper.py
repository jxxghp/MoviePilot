import time
from typing import Optional, Union

from sqlalchemy.orm import Session

from app.db import DbOper
from app.db.models.message import Message
from app.schemas import MessageChannel, NotificationType


class MessageOper(DbOper):
    """
    消息数据管理
    """

    def __init__(self, db: Session = None):
        super().__init__(db)

    def add(self,
            channel: MessageChannel = None,
            source: Optional[str] =  None,
            mtype: NotificationType = None,
            title: Optional[str] =  None,
            text: Optional[str] =  None,
            image: Optional[str] =  None,
            link: Optional[str] =  None,
            userid: Optional[str] =  None,
            action: Optional[int] =  1,
            note: Union[list, dict] = None,
            **kwargs):
        """
        新增媒体服务器数据
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
            if k not in Message.__table__.columns.keys(): # noqa
                kwargs.pop(k)

        Message(**kwargs).create(self._db)

    def list_by_page(self, page: Optional[int] =  1, count: Optional[int] =  30) -> Optional[str]:
        """
        获取媒体服务器数据ID
        """
        return Message.list_by_page(self._db, page, count)
