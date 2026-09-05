"""存储授权 API 输出模型。"""

from typing import Optional

from pydantic import BaseModel, Field


class StorageQrCodeData(BaseModel):
    """云存储扫码授权二维码。"""

    codeContent: Optional[str] = Field(default=None, description="二维码原始内容")
    codeUrl: Optional[str] = Field(default=None, description="二维码图片地址")


class StorageAuthUrlData(BaseModel):
    """云存储 OAuth 授权入口。"""

    authUrl: str = Field(description="授权地址")
    state: str = Field(description="授权状态校验值")


class StorageLoginStatusData(BaseModel):
    """云存储扫码或 OAuth 登录状态。"""

    status: int | str = Field(description="授权状态")
    tip: str = Field(description="状态提示")


class StorageOption(BaseModel):  # type: ignore[misc]
    """前端选择控件可安全消费的存储摘要。"""

    name: str = Field(description="存储显示名称")
    type: str = Field(description="存储类型标识")
