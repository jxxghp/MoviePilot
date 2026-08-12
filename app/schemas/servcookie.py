from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import JsonData


class CookieData(BaseModel):
    """CookieCloud 上传的加密数据。"""

    encrypted: str = Field(min_length=1, max_length=1024 * 1024 * 50)
    uuid: str = Field(min_length=5, pattern="^[a-zA-Z0-9]+$")


class CookiePassword(BaseModel):
    """CookieCloud 下载并解密数据所需的密码。"""

    password: str


class CookieActionResponse(BaseModel):
    """CookieCloud 上传操作结果。"""

    action: Literal["done", "error"]


class CookieEncryptedPayload(BaseModel):
    """CookieCloud 保存和下载的加密载荷。"""

    encrypted: str


class CookieDecryptedPayload(BaseModel):
    """CookieCloud 解密后的 Cookie 数据载荷。"""

    model_config = ConfigDict(extra="allow")

    cookie_data: JsonData
