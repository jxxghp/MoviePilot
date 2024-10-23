from typing import Optional

from pydantic import BaseModel, Field, root_validator


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


class AuthCredentials(ChainEventData):
    """
    AuthVerification 事件的数据模型

    Attributes:
        username (Optional[str]): 用户名，适用于 "password" grant_type
        password (Optional[str]): 用户密码，适用于 "password" grant_type
        mfa_code (Optional[str]): 一次性密码，目前仅适用于 "password" 认证类型
        code (Optional[str]): 授权码，适用于 "authorization_code" grant_type
        grant_type (str): 认证类型，如 "password", "authorization_code", "client_credentials"
        # scope (List[str]): 权限范围，如 ["read", "write"]
        token (Optional[str]): 认证令牌
        channel (Optional[str]): 认证渠道
        service (Optional[str]): 服务名称
    """
    # 输入参数
    username: Optional[str] = Field(None, description="用户名，适用于 'password' 认证类型")
    password: Optional[str] = Field(None, description="用户密码，适用于 'password' 认证类型")
    mfa_code: Optional[str] = Field(None, description="一次性密码，目前仅适用于 'password' 认证类型")
    code: Optional[str] = Field(None, description="授权码，适用于 'authorization_code' 认证类型")
    grant_type: str = Field(..., description="认证类型，如 'password', 'authorization_code', 'client_credentials'")
    # scope: List[str] = Field(default_factory=list, description="权限范围，如 ['read', 'write']")

    # 输出参数
    # grant_type 为 authorization_code 时，输出参数包括 username、token、channel、service
    token: Optional[str] = Field(None, description="认证令牌")
    channel: Optional[str] = Field(None, description="认证渠道")
    service: Optional[str] = Field(None, description="服务名称")

    @root_validator(pre=True)
    def check_fields_based_on_grant_type(cls, values):
        grant_type = values.get("grant_type")
        if not grant_type:
            values["grant_type"] = "password"
            grant_type = "password"

        if grant_type == "password":
            if not values.get("username") or not values.get("password"):
                raise ValueError("username and password are required for grant_type 'password'")

        elif grant_type == "authorization_code":
            if not values.get("code"):
                raise ValueError("code is required for grant_type 'authorization_code'")

        return values


class AuthInterceptCredentials(ChainEventData):
    """
    AuthPassedIntercept 事件的数据模型

    Attributes:
        # 输入参数
        username (str): 用户名
        channel (str): 认证渠道
        service (str): 服务名称
        token (str): 认证令牌
        status (str): 认证状态，"triggered" 和 "completed" 两个状态

        # 输出参数
        source (str): 拦截源，默认值为 "未知拦截源"
        cancel (bool): 是否取消认证，默认值为 False
    """
    # 输入参数
    username: Optional[str] = Field(..., description="用户名")
    channel: str = Field(..., description="认证渠道")
    service: str = Field(..., description="服务名称")
    status: str = Field(..., description="认证状态, 包含 'triggered' 表示认证触发，'completed' 表示认证成功")
    token: Optional[str] = Field(None, description="认证令牌")

    # 输出参数
    source: str = Field("未知拦截源", description="拦截源")
    cancel: bool = Field(False, description="是否取消认证")
