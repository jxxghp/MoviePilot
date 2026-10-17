"""
接口通用结果、错误详情和分页响应的数据结构。
"""

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic import Field as _Field

from app.runtime.localization import LocaleHelper

DataT = TypeVar("DataT")


class Response(BaseModel, Generic[DataT]):
    """
    统一接口响应结构，仅允许业务数据类型随接口变化。
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["success", "message", "data"]}
    )

    success: bool = _Field(description="状态")
    message: str = _Field(default="", description="消息文本")
    data: Optional[DataT] = _Field(default=None, description="数据")

    @field_validator("message", mode="before")
    @classmethod
    def localize_message(cls, value: Any) -> str:
        """
        先移除内部实现术语，再按当前请求语言本地化消息文本。
        """
        if value is None:
            return ""
        raw_message = str(value)
        if not raw_message.strip():
            return ""
        from app.runtime.errors import public_error_message

        message = public_error_message(raw_message)
        if not message:
            return ""
        return LocaleHelper.translate_text(
            message, locale=LocaleHelper.get_current_locale()
        )


class ValidationIssue(BaseModel):
    """
    请求参数校验失败时返回的单项错误信息。
    """

    location: list[str | int] = _Field(description="参数位置")
    message: str = _Field(description="错误说明")
    error_type: str = _Field(description="错误类型")
