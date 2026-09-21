"""GitHub 授权边界使用的领域协议与不可变数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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


class GithubAuthPort(Protocol):
    """GitHub OAuth 外部协议所需的最小传输端口。"""

    async def request_device_code(self, client_id: str) -> GithubDeviceCode:
        """申请一个 GitHub Device Flow 设备码。"""
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
