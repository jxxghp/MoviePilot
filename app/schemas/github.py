"""GitHub Token 授权接口的数据模型。"""

from typing import Literal

from pydantic import BaseModel, Field


class GithubTokenStatus(BaseModel):  # type: ignore[misc]
    """描述当前 GitHub Token 的可用状态，不包含 Token 原文。"""

    configured: bool = Field(description="是否已配置 GitHub Token")
    valid: bool | None = Field(default=None, description="最近一次校验是否有效")
    source: Literal["oauth", "manual"] | None = Field(
        default=None,
        description="Token 来源：OAuth 设备授权或手动填写",
    )
    login: str | None = Field(default=None, description="授权 GitHub 用户名")
    masked_token: str | None = Field(default=None, description="脱敏后的 Token 摘要")
    expires_at: int | None = Field(default=None, description="Token 过期时间戳")
    needs_reauthorization: bool = Field(
        default=False,
        description="Token 是否失效、临近过期或缺少 Agent 提交 Issue/PR 所需授权范围",
    )


class GithubDeviceAuthStart(BaseModel):  # type: ignore[misc]
    """返回 GitHub Device Flow 的浏览器操作信息。"""

    session_id: str = Field(description="服务端设备授权会话标识")
    verification_uri: str = Field(description="GitHub 验证页面")
    user_code: str = Field(description="需要在 GitHub 页面输入的用户码")
    expires_in: int = Field(description="设备码有效秒数")
    interval_seconds: int = Field(description="建议的轮询间隔秒数")


class GithubDeviceAuthPollRequest(BaseModel):  # type: ignore[misc]
    """轮询 GitHub Device Flow 所需的会话标识。"""

    session_id: str = Field(min_length=1, max_length=128)


class GithubDeviceAuthPoll(BaseModel):  # type: ignore[misc]
    """返回设备授权当前阶段，授权成功时附带脱敏状态。"""

    state: Literal["pending", "authorized", "slow_down", "denied", "expired", "failed"]
    message: str = ""
    retry_after: int | None = Field(default=None, description="下一次轮询建议等待秒数")
    status: GithubTokenStatus | None = Field(default=None, description="授权成功后的 Token 状态")


class GithubManualTokenRequest(BaseModel):  # type: ignore[misc]
    """兼容手动填写 GitHub Token 的请求体。"""

    token: str = Field(min_length=1, max_length=512)
