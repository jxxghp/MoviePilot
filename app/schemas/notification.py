"""通知渠道 API 输出模型。"""

from typing import Optional

from pydantic import BaseModel, Field


class WechatClawBotKnownTarget(BaseModel):
    """微信 ClawBot 已知消息目标。"""

    userid: str
    username: str
    last_active: Optional[int | float] = None


class WechatClawBotData(BaseModel):
    """微信 ClawBot 登录状态或操作结果。"""

    success: bool
    message: Optional[str] = None
    connected: Optional[bool] = None
    account_id: Optional[str] = None
    qrcode: Optional[str] = None
    qrcode_url: Optional[str] = None
    qrcode_status: Optional[str] = None
    qrcode_updated_at: Optional[int | float] = None
    known_targets: list[WechatClawBotKnownTarget] = Field(default_factory=list)
    default_target: Optional[str] = None
    base_url: Optional[str] = None
