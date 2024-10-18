from typing import Optional

from pydantic import BaseModel, Field


class BaseEventData(BaseModel):
    """
    事件数据的基类，所有具体事件数据类应继承自此类
    """
    pass


class ChainEventData(BaseEventData):
    """
    链式事件数据的基类，所有具体事件数据类应继承自此类
    """
    pass


class AuthVerificationData(ChainEventData):
    """
    AuthVerification 事件的数据模型

    Attributes:
        # 输入参数
        name (str): 用户名
        password (str): 用户密码

        # 输出参数
        token (str): 认证令牌
        channel (str): 认证渠道
        service (str): 服务名称
    """
    # 输入参数
    name: str = Field(..., description="用户名")
    password: str = Field(..., description="用户密码")

    # 输出参数
    token: Optional[str] = Field(None, description="认证令牌")
    channel: Optional[str] = Field(None, description="认证渠道")
    service: Optional[str] = Field(None, description="服务名称")


class AuthPassedInterceptData(ChainEventData):
    """
    AuthPassedIntercept 事件的数据模型。

    Attributes:
        # 输入参数
        name (str): 用户名
        channel (str): 认证渠道
        service (str): 服务名称
        token (str): 认证令牌

        # 输出参数
        source (str): 拦截源，默认值为 "未知拦截源"
        cancel (bool): 是否取消认证，默认值为 False
    """
    # 输入参数
    name: str = Field(..., description="用户名")
    channel: str = Field(..., description="认证渠道")
    service: str = Field(..., description="服务名称")
    token: Optional[str] = Field(None, description="认证令牌")

    # 输出参数
    source: str = Field("未知拦截源", description="拦截源")
    cancel: bool = Field(False, description="是否取消认证")
