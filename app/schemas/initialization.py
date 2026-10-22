"""
首次初始化接口使用的请求与状态模型。
"""

import re

from pydantic import BaseModel, Field, field_validator, model_validator


class InitializationStatus(BaseModel):
    """
    描述 MoviePilot 的首次初始化状态。

    未初始化且部署配置已提供 SUPERUSER 时，同时返回固定的管理员用户名，供初始化页面锁定输入。
    """

    initialized: bool = Field(description="是否已经完成首次初始化")
    configured_username: str | None = Field(default=None, description="部署配置指定的管理员名，未指定时为空")


class InitializationRequest(BaseModel):
    """
    首次初始化超级管理员、密码和 API Key 所需的提交数据。
    """

    username: str = Field(min_length=1, max_length=50, description="首次创建的超级管理员用户名，长度为 1 至 50 个字符")
    password: str = Field(min_length=6, max_length=50, description="管理员密码，长度为 6 至 50 个字符，并包含字母、数字、特殊字符中的至少两类")
    confirm_password: str = Field(min_length=6, max_length=50, description="再次输入的管理员密码，必须与 password 完全一致")
    api_key: str = Field(min_length=16, max_length=256, description="接口访问密钥，长度为 16 至 256 个字符")

    @field_validator("username", "api_key", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """
        去除用户名和 API Key 的首尾空白，避免产生不可见凭据差异。
        """
        return value.strip() if isinstance(value, str) else value

    @field_validator("password", "confirm_password")
    @classmethod
    def validate_password_shape(cls, value: str) -> str:
        """
        复用用户管理中的密码复杂度约束。
        """
        if not re.match(r"^(?![a-zA-Z]+$)(?!\d+$)(?![^\da-zA-Z\s]+$).{6,50}$", value):
            raise ValueError("密码需要同时包含字母、数字、特殊字符中的至少两项，且长度为 6-50 位")
        return value

    @model_validator(mode="after")
    def passwords_match(self) -> "InitializationRequest":
        """
        确保两次输入的密码完全一致。
        """
        if self.password != self.confirm_password:
            raise ValueError("两次输入的密码不一致")
        return self
