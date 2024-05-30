
from pydantic import BaseModel

from app.schemas.types import MessageChannel, NotificationType


class CommingMessage(BaseModel):
    """
    外来消息
    """
    # 用户ID
    userid: str | int | None = None
    # 用户名称
    username: str | None = None
    # 消息渠道
    channel: MessageChannel | None = None
    # 消息体
    text: str | None = None
    # 时间
    date: str | None = None
    # 消息方向
    action: int | None = 0

    def to_dict(self):
        """
        转换为字典
        """
        items = self.dict()
        for k, v in items.items():
            if isinstance(v, MessageChannel):
                items[k] = v.value
        return items


class Notification(BaseModel):
    """
    消息
    """
    # 消息渠道
    channel: MessageChannel | None = None
    # 消息类型
    mtype: NotificationType | None = None
    # 标题
    title: str | None = None
    # 文本内容
    text: str | None = None
    # 图片
    image: str | None = None
    # 链接
    link: str | None = None
    # 用户ID
    userid: str | int | None = None
    # 时间
    date: str | None = None
    # 消息方向
    action: int | None = 1

    def to_dict(self):
        """
        转换为字典
        """
        items = self.dict()
        for k, v in items.items():
            if isinstance(v, MessageChannel) \
                    or isinstance(v, NotificationType):
                items[k] = v.value
        return items


class NotificationSwitch(BaseModel):
    """
    消息开关
    """
    # 消息类型
    mtype: str | None = None
    # 微信开关
    wechat: bool | None = False
    # TG开关
    telegram: bool | None = False
    # Slack开关
    slack: bool | None = False
    # SynologyChat开关
    synologychat: bool | None = False
    # VoceChat开关
    vocechat: bool | None = False
