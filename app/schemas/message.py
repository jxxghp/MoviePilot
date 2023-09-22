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
