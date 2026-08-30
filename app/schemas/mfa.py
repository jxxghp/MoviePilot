"""多因素认证 API 输出模型。"""

from typing import Optional

from pydantic import BaseModel, Field, RootModel

from app.schemas.common import JsonData


class PasskeyOptions(RootModel[dict[str, JsonData]]):
    """浏览器 WebAuthn API 使用的动态选项。"""


class OtpGenerateData(BaseModel):
    """OTP 绑定密钥和验证 URI。"""

    secret: str = Field(description="OTP 密钥")
    uri: str = Field(description="OTP 验证 URI")


class PasskeyStartData(BaseModel):
    """PassKey 注册或认证的启动数据。"""

    options: PasskeyOptions = Field(description="WebAuthn 选项")
    transaction_token: str = Field(description="一次性事务令牌")


class PasskeyInfo(BaseModel):
    """当前用户绑定的 PassKey 摘要。"""

    id: int
    name: str
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None
    aaguid: Optional[str] = None
    transports: Optional[str] = None
