"""
GitHub Token 授权接口的数据模型。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class GithubCommunityProfile(BaseModel):
    """
    实例 Token 对应 GitHub 用户的社区勋章快照，不包含凭据。

    state 为 disabled 表示社区勋章对当前实例未生效：本机 COMMUNITY_MEDALS_ENABLE 关闭，或
    MoviePilot Server 未开放（全局开关关闭且当前 GitHub 用户不在预览名单、未登录 GitHub 或服务端
    不可达）；此时不核验
    身份、不拉取 Wiki 名单，其余字段均为默认值，前端应隐藏全部勋章入口。功能已开放但实例未配置
    GitHub Token 时为 unconfigured，前端可提示配置 Token。
    """

    state: Literal["disabled", "unconfigured", "ready", "unavailable"] = Field(
        description=(
            "disabled 本机关闭或服务端未开放社区勋章，前端应隐藏；unconfigured 功能已开放但未配置 Token；"
            "ready 已核对名单；unavailable 身份失效或名单从未取得"
        ),
    )
    login: str | None = Field(default=None, description="Token 对应的 GitHub 登录名，仅用于展示")
    user_id: int | None = Field(default=None, description="Token 对应的 GitHub 数字 ID，勋章按此查表")
    medals: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "该 ID 在 Wiki 名单中的勋章记录，原样透传且省略的字段不补 null，未登记时为空。"
            "已知字段：key 勋章键；tier 阶梯等级 1-4（初光、共建、卓越、核心）；specialty 编剧（code）领域称号；"
            "at 刻印日期 YYYY-MM-DD 或年份 YYYY；basis 阶梯时间口径 reached 或 recognized；"
            "years 保留的整数年份列表（人工勋章当前只用 at）；note 人工勋章贡献说明；"
            "serial 编号（当前为 null）；facts 阶梯勋章展示依据。未认识的键与字段供新版前端使用"
        ),
    )
    generated_at: str | None = Field(default=None, description="Wiki 名单的生成时间（ISO 8601 UTC）")
    checked_at: int | None = Field(default=None, description="实例最近一次成功核对名单的时间戳")
    stale: bool = Field(default=False, description="最近一次刷新失败，当前为同一 Token 的上次结果")
    refreshing: bool = Field(default=False, description="后台正在预热或更新；接口已立即返回当前快照")


class GithubTokenStatus(BaseModel):
    """
    描述当前 GitHub Token 的可用状态，不包含 Token 原文。
    """

    configured: bool = Field(description="是否已配置 GitHub Token")
    valid: bool | None = Field(default=None, description="最近一次校验是否有效")
    source: Literal["oauth", "manual"] | None = Field(
        default=None,
        description="Token 来源：OAuth 设备授权或手动填写",
    )
    login: str | None = Field(default=None, description="授权 GitHub 用户名")
    masked_token: str | None = Field(default=None, description="脱敏后的 Token 摘要")
    expires_at: int | None = Field(default=None, description="Token 过期的 Unix 时间戳，单位为秒；未知或无过期时间时为 None")
    needs_reauthorization: bool = Field(
        default=False,
        description="Token 是否失效、临近过期或缺少 Agent 提交 Issue/PR 所需授权范围",
    )


class GithubDeviceAuthStart(BaseModel):
    """
    返回 GitHub Device Flow 的浏览器操作信息。
    """

    session_id: str = Field(description="服务端设备授权会话标识")
    verification_uri: str = Field(description="GitHub 验证页面")
    user_code: str = Field(description="需要在 GitHub 页面输入的用户码")
    expires_in: int = Field(description="设备码有效秒数")
    interval_seconds: int = Field(description="建议的轮询间隔秒数")


class GithubDeviceAuthPollRequest(BaseModel):
    """
    轮询 GitHub Device Flow 所需的会话标识。
    """

    session_id: str = Field(min_length=1, max_length=128, description="待查询的 GitHub 设备授权会话标识")


class GithubDeviceAuthPoll(BaseModel):
    """
    返回设备授权当前阶段，授权成功时附带脱敏状态。
    """

    state: Literal["pending", "authorized", "slow_down", "denied", "expired", "failed"] = Field(
        description="设备授权阶段：等待、成功、减慢轮询、拒绝、过期或失败",
    )
    message: str = Field(default="", description="面向调用方的结果说明")
    retry_after: int | None = Field(default=None, description="下一次轮询建议等待秒数")
    status: GithubTokenStatus | None = Field(default=None, description="授权成功后的 Token 状态")


class GithubManualTokenRequest(BaseModel):
    """
    兼容手动填写 GitHub Token 的请求体。
    """

    token: str = Field(min_length=1, max_length=512, description="用户手动填写的 GitHub 访问令牌")
