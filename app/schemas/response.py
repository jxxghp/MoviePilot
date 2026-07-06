from typing import Optional, Union

from pydantic import BaseModel, Field, model_validator

from app.helper.locale import LocaleHelper


class Response(BaseModel):
    """通用接口响应结构"""

    # 状态
    success: bool
    # 消息文本
    message: Optional[str] = None
    # 多语言消息文本
    message_i18n: Optional[str] = None
    # 数据
    data: Optional[Union[dict, list]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fill_message_i18n(self) -> "Response":
        """
        自动补充响应消息的多语言文本。
        """
        if self.message and self.message_i18n is None:
            self.message_i18n = LocaleHelper.translate_text(
                self.message, locale=LocaleHelper.get_current_locale()
            )
        return self
