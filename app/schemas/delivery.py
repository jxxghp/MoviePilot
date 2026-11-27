"""
单个投递目标或消息分段的确认结果。
"""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.types import NotificationChannel

type DeliveryStatus = Literal["confirmed", "retryable_failure", "permanent_failure", "skipped", "unknown"]
type DeliveryReceipt = Callable[[str | None, int, DeliveryStatus, str | None], None]
type DeliveryPartPlan = tuple[tuple[str, int, str], ...]
type DeliveryPartPlanner = Callable[[DeliveryPartPlan], None]
type DeliveryPartOutcome = (
    tuple[DeliveryStatus, str | None]
    | tuple[DeliveryStatus, str | None, str]
    | tuple[DeliveryStatus, str | None, str | None, int | None]
)
type DeliveryPartSubmitter = Callable[[], DeliveryPartOutcome]
type DeliveryPartSender = Callable[
    [str, int, str, bool, DeliveryPartSubmitter],
    tuple[DeliveryStatus, str | None],
]


class DeliveryResult(BaseModel):
    """
    保留适配器确认过的局部结果，用于追踪部分完成的投递。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: NotificationChannel = Field(description="执行投递的渠道平台")
    connection_id: str = Field(description="执行投递的稳定连接 ID")
    identity_namespace: str | None = Field(default=None, description="此次投递所在的连接身份空间")
    target_id: str | None = Field(default=None, description="该连接内明确指定的目标标识")
    part: int = Field(default=0, ge=0, description="从零开始的消息分段序号")
    content_digest: str | None = Field(default=None, description="提交前用于比对此分段内容的摘要")
    status: DeliveryStatus = Field(description="该目标和分段的投递结果")
    message_id: str | None = Field(default=None, description="平台确认后返回的消息标识")
    code: str | None = Field(default=None, description="固定错误类别，不含外部返回的正文或凭据")
    retry_after: int | None = Field(default=None, ge=0, description="平台建议等待的秒数")
