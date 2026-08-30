"""首次初始化接口使用的请求与状态模型。"""

import re

from pydantic import BaseModel, Field, field_validator, model_validator


class InitializationStatus(BaseModel):  # type: ignore[misc]
    """描述 MoviePilot 是否已经存在可用的本地用户记录。"""

    initialized: bool


class InitializationRequest(BaseModel):  # type: ignore[misc]
    """首次初始化超级管理员、密码和 API Key 所需的提交数据。"""

    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=6, max_length=50)
    confirm_password: str = Field(min_length=6, max_length=50)
    api_key: str = Field(min_length=16, max_length=256)

    @field_validator("username", "api_key")  # type: ignore[misc]
    @classmethod
    def strip_text(cls, value: str) -> str:
        """去除用户名和 API Key 的首尾空白，避免产生不可见凭据差异。"""
        return value.strip()

    @field_validator("password", "confirm_password")  # type: ignore[misc]
    @classmethod
    def validate_password_shape(cls, value: str) -> str:
        """复用用户管理中的密码复杂度约束。"""
        if not re.match(r"^(?![a-zA-Z]+$)(?!\d+$)(?![^\da-zA-Z\s]+$).{6,50}$", value):
            raise ValueError("密码需要同时包含字母、数字、特殊字符中的至少两项，且长度为 6-50 位")
        return value

    @model_validator(mode="after")  # type: ignore[misc]
    def passwords_match(self) -> "InitializationRequest":
        """确保两次输入的密码完全一致。"""
        if self.password != self.confirm_password:
            raise ValueError("两次输入的密码不一致")
        return self
