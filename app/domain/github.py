"""GitHub 授权边界使用的领域协议与不可变数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# PR Skill 需要读写仓库并推送工作流文件，反馈 Skill 需要创建 Issue。
GITHUB_DEVICE_AUTH_SCOPES = ("read:user", "repo", "workflow")


class GithubAuthTransportError(RuntimeError):
    """表示 GitHub 授权外部传输或响应协议失败。"""

    def __init__(self, message: str, *, unauthorized: bool = False) -> None:
        """保存不含外部响应原文的错误信息及 Token 失效标记。"""
        super().__init__(message)
        self.unauthorized = unauthorized


@dataclass(frozen=True, slots=True)
class GithubDeviceCode:
    """GitHub Device Flow 返回的设备码信息。"""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval_seconds: int


@dataclass(frozen=True, slots=True)
class GithubTokenExchange:
    """GitHub 设备码或刷新请求的规范化结果。"""

    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    refresh_token_expires_in: int | None = None
    scope: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GithubUser:
    """GitHub 当前用户的最小身份摘要。"""

    login: str
    user_id: int = 0


# 仓库与统计分支由社区维护，调用方不能把任意外部仓库注入授权请求。
COMMUNITY_REPOSITORIES = (
    ("MoviePilot", "backend", "v3"),
    ("MoviePilot-Frontend", "frontend", "v3"),
    ("MoviePilot-Plugins", "plugins", "main"),
    ("MoviePilot-Rust", "performance", "v3"),
    ("MoviePilot-Resources", "resources", "main"),
    ("MoviePilot-Server", "services", "main"),
    ("MoviePilot-OCR", "services", "main"),
    ("MoviePilot-Web", "website", "main"),
    ("MoviePilot-Wiki", "docs", "main"),
)


@dataclass(frozen=True, slots=True)
class GithubRepositoryContribution:
    """保留已合并 PR 与指定分支作者提交数，避免把两者相加重复奖励。"""

    repository: str
    area: str
    commits: int
    merged_prs: int

    @property
    def units(self) -> int:
        """同仓取两类证据较大值，兼顾直接提交与被合并的贡献。"""
        return max(self.commits, self.merged_prs)


@dataclass(frozen=True, slots=True)
class GithubCommunityProfile:
    """社区荣誉的脱敏快照；不可用与确证零贡献是不同状态。"""

    state: str = "unconfigured"
    login: str | None = None
    user_id: int | None = None
    tier: str | None = None
    specialty: str | None = None
    total: int = 0
    repositories: tuple[GithubRepositoryContribution, ...] = ()
    checked_at: int | None = None
    stale: bool = False


class GithubCommunityPort(Protocol):
    """查询已认证 GitHub 身份及社区贡献证据的外部端口。"""

    async def get_user(self, access_token: str) -> GithubUser:
        """使用 Token 校验身份，不接受前端声明的 GitHub 用户名。"""
        ...

    async def get_repository_contribution(
        self, access_token: str, user: GithubUser, repository: str, area: str, branch: str,
    ) -> GithubRepositoryContribution:
        """取得固定社区仓库的已合并 PR 数与目标分支作者提交数。"""
        ...


class GithubAuthPort(Protocol):
    """GitHub OAuth 外部协议所需的最小传输端口。"""

    async def request_device_code(
        self,
        client_id: str,
        scope: str = "read:user",
    ) -> GithubDeviceCode:
        """按调用方 scope 申请设备码，默认保留 Copilot 登录的只读用户权限。"""
        ...

    async def exchange_device_code(
        self,
        client_id: str,
        device_code: str,
    ) -> GithubTokenExchange:
        """交换设备码并返回授权状态或 Token。"""
        ...

    async def refresh_access_token(
        self,
        client_id: str,
        refresh_token: str,
    ) -> GithubTokenExchange:
        """使用 GitHub OAuth 刷新 Token。"""
        ...

    async def get_user(self, access_token: str) -> GithubUser:
        """校验 Token 并读取 GitHub 登录名。"""
        ...


class GithubSettingsReader(Protocol):
    """为 GitHub 网络适配器提供最小只读部署设置接口。"""

    def get(self, key: str, default: Any = None) -> Any:
        """读取一个部署设置值。"""
        ...
