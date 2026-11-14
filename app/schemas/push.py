"""
原生容器的通知管理请求，不允许指定其他用户或提交中转发送密钥。
"""

from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AppPushInstanceRequest(BaseModel):
    """
    仅容器管理员可轮换实例凭据，普通用户的设备管理请求不能触发该动作。
    """

    model_config = ConfigDict(extra="forbid")
    action: Literal["rotate"] = Field(description="轮换当前已配置中心的自动接入凭据")
    source: str = Field(min_length=1, max_length=255, description="已保存并启用的 AppPush 渠道名称")


class AppPushRequest(BaseModel):
    """
    通知操作绑定当前登录身份；来源由启用的通知渠道配置选择。
    """

    model_config = ConfigDict(extra="forbid")
    action: Literal["status", "register", "unregister", "pair", "test"] = Field(description="通知管理动作")
    installation: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$", description="App 安装的随机标识")
    origin: str = Field(min_length=1, max_length=2048, description="App 正在连接的实例地址")
    token: Optional[str] = Field(default=None, min_length=32, max_length=512, pattern=r"^[0-9a-fA-F]+$", description="系统 APNs 令牌，仅注册时使用")
    topic: Optional[str] = Field(default=None, min_length=1, max_length=255, description="签名 Bundle ID")
    environment: Literal["sandbox", "production"] = Field(default="sandbox", description="签名对应的 APNs 环境")
    source: Optional[str] = Field(default=None, max_length=255, description="指定启用的 AppPush 渠道名称，省略时选第一个")

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        """
        只允许不带账号、查询或片段的 HTTP(S) 实例地址。
        """
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("实例地址无效")
        return value.rstrip("/")
