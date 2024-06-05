from typing import Optional, Union

from pydantic import BaseModel

from app.schemas.types import NotificationType, MessageChannel


class CommingMessage(BaseModel):
    """
    外来消息
    """
    # 用户ID
    userid: Optional[Union[str, int]] = None
    # 用户名称
    username: Optional[str] = None
    # 消息渠道
    channel: Optional[MessageChannel] = None
    # 消息体
    text: Optional[str] = None
    # 时间
    date: Optional[str] = None
    # 消息方向
    action: Optional[int] = 0

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
    channel: Optional[MessageChannel] = None
    # 消息类型
    mtype: Optional[NotificationType] = None
    # 标题
    title: Optional[str] = None
    # 文本内容
    text: Optional[str] = None
    # 图片
    image: Optional[str] = None
    # 链接
    link: Optional[str] = None
    # 用户ID
    userid: Optional[Union[str, int]] = None
    # 时间
    date: Optional[str] = None
    # 消息方向
    action: Optional[int] = 1

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
    mtype: Optional[str] = None
    # 微信开关
    wechat: Optional[bool] = False
    # TG开关
    telegram: Optional[bool] = False
    # Slack开关
    slack: Optional[bool] = False
    # SynologyChat开关
    synologychat: Optional[bool] = False
    # VoceChat开关
    vocechat: Optional[bool] = False
    # WebPush开关
    webpush: Optional[bool] = False


class Subscription(BaseModel):
    """
    客户端消息订阅
    """
    endpoint: Optional[str]
    keys: Optional[dict] = {}


class SubscriptionMessage(BaseModel):
    """
    客户端订阅消息体
    """
    title: Optional[str]
    body: Optional[str]
    icon: Optional[str]
    url: Optional[str]
    data: Optional[dict] = {}
