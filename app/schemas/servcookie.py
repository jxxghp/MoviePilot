"""
CookieCloud 登录凭据交换和操作结果的数据结构。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import JsonData


class CookieData(BaseModel):
    """
    CookieCloud 上传的加密数据。
    """

    encrypted: str = Field(min_length=1, max_length=1024 * 1024 * 50, description="加密后的登录凭据内容")
    uuid: str = Field(min_length=5, pattern="^[a-zA-Z0-9]+$", description="CookieCloud 用户标识")


class CookiePassword(BaseModel):
    """
    CookieCloud 下载并解密数据所需的密码。
    """

    password: str = Field(description="用于解密 CookieCloud 数据的密码")


class CookieActionResponse(BaseModel):
    """
    CookieCloud 上传操作结果。
    """

    action: Literal["done", "error"] = Field(description="上传结果，done 表示完成，error 表示失败")


class CookieEncryptedPayload(BaseModel):
    """
    CookieCloud 保存和下载的加密载荷。
    """

    encrypted: str = Field(description="加密后的登录凭据内容")


class CookieDecryptedPayload(BaseModel):
    """
    CookieCloud 解密后的 Cookie 数据载荷。
    """

    model_config = ConfigDict(extra="allow")

    cookie_data: JsonData = Field(description="按域名组织的解密后登录凭据")
