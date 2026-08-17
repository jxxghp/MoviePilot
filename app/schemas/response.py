from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.i18n import translate


DataT = TypeVar("DataT")


class Response(BaseModel, Generic[DataT]):
    """统一接口响应结构，仅允许业务数据类型随接口变化。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["success", "message", "data"]}
    )

    # 状态
    success: bool
    # 消息文本
    message: str = ""
    # 数据
    data: Optional[DataT] = None

    @field_validator("message", mode="before")
    @classmethod
    def localize_message(cls, value: Any) -> str:
        """按当前请求语言直接本地化消息文本，并将空消息归一为空字符串。"""
        if value is None:
            return ""
        message = str(value)
        if not message:
            return ""
        return translate(message)


class ValidationIssue(BaseModel):
    """请求参数校验失败时返回的单项错误信息。"""

    # 参数位置
    location: list[str | int]
    # 错误说明
    message: str
    # 错误类型
    error_type: str
