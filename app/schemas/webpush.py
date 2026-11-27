"""
浏览器订阅持有证明的严格 HTTP 合同，客户端不能指定账号归属。
"""

from typing import Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WebPushKeys(BaseModel):
    """
    浏览器 PushSubscription 提供的两项完整密钥。

    :ivar p256dh: 浏览器生成的推送公钥，以 URL 安全 Base64 表示
    :ivar auth: 浏览器持有的认证密钥，以 URL 安全 Base64 表示
    """

    model_config = ConfigDict(extra="forbid")
    p256dh: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+={0,2}$", description="浏览器推送公钥")
    auth: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+={0,2}$", description="浏览器推送认证密钥")


class WebPushSubscription(BaseModel):
    """
    当前浏览器订阅；owner 始终由服务器当前账号注入。

    :ivar endpoint: 浏览器推送服务的原始 HTTPS 地址
    :ivar keys: 当前浏览器生成的完整公钥和认证密钥
    :ivar expiration_time: 兼容浏览器原生 toJSON 的过期时间；不参与账号归属或存储
    """

    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=4096, description="浏览器推送服务的 HTTPS 端点")
    keys: WebPushKeys = Field(description="浏览器当前订阅的完整公钥和认证密钥")
    expiration_time: Optional[int] = Field(default=None, alias="expirationTime", description="浏览器原生订阅过期时间，不参与服务端归属")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """
        拒绝非 HTTPS、凭据、片段及控制字符，不改写浏览器端点身份。

        :param value: 浏览器提交的原始端点
        :return: 通过校验的原始端点
        :raises ValueError: 端点不是完整 HTTPS 推送地址
        """
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("请输入有效的 HTTPS 推送端点")
        return value
