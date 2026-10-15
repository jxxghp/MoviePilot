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


# Wiki 名单的协议版本；结构不兼容时由名单方提升版本，旧实例按拉取失败处理而不是误读字段。
COMMUNITY_MEDAL_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class GithubMedalSnapshot:
    """Wiki 勋章名单中某个 GitHub 数字 ID 的记录与名单生成时间。

    ``medals`` 保留名单原始记录（已在协议边界校验），未认识的勋章键与字段原样透传，
    名单中没有该 ID 时为空元组，表示已核对但暂无勋章。
    """

    generated_at: str
    medals: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class GithubCommunityProfile:
    """社区勋章的脱敏快照；不可用与确证暂无勋章是不同状态。

    state 取值：unconfigured、ready、unavailable，以及本机或服务端开关未生效时的 disabled。
    """

    state: str = "unconfigured"
    login: str | None = None
    user_id: int | None = None
    medals: tuple[dict[str, Any], ...] = ()
    generated_at: str | None = None
    checked_at: int | None = None
    stale: bool = False


class GithubCommunityPort(Protocol):
    """核验 GitHub 身份并读取 Wiki 社区勋章名单的外部端口。"""

    async def get_user(self, access_token: str) -> GithubUser:
        """使用 Token 校验身份，不接受前端声明的 GitHub 用户名。"""
        ...

    async def get_medal_snapshot(self, user_id: int) -> GithubMedalSnapshot:
        """拉取公开勋章名单并取出指定数字 ID 的记录；名单不可用或格式不符时抛出传输错误。"""
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
