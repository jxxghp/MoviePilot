"""GitHub OAuth Device Flow 的网络适配器。"""

from __future__ import annotations

from typing import Any

from app.adapters.network.http import AsyncRequestUtils
from app.domain.github import (
    GithubAuthPort,
    GithubAuthTransportError,
    GithubDeviceCode,
    GithubSettingsReader,
    GithubTokenExchange,
    GithubUser,
)
from app.runtime.settings import get_runtime_setting

# Device Flow 的 client_id 本身不是秘密；沿用当前内置 Copilot 登录使用的公开应用标识，
# 后续如果注册专用 OAuth App，只需在组合根替换这一常量，不会影响授权协议和存储合同。
GITHUB_DEVICE_CLIENT_ID = "Ov23li8tweQw6odWQebz"


class _RuntimeGithubSettings:
    """把 runtime 设置读取端口适配为 GitHub 客户端所需的 get 接口。"""

    def get(self, key: str, default: Any = None) -> Any:
        """读取一个运行时设置，并在启动早期保留默认值语义。"""
        return get_runtime_setting(key, default)


class GithubAuthClient(GithubAuthPort):
    """通过 MoviePilot 统一异步 HTTP 适配器访问 GitHub OAuth API。"""

    _DEVICE_CODE_URL = "https://github.com/login/device/code"
    _ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
    _USER_URL = "https://api.github.com/user"

    def __init__(self, settings: GithubSettingsReader | None = None) -> None:
        """保存代理、User-Agent 等部署设置提供器。"""
        self._settings = settings or _RuntimeGithubSettings()

    async def request_device_code(
        self,
        client_id: str,
        scope: str = "read:user",
    ) -> GithubDeviceCode:
        """按调用方 scope 请求设备码，默认保留 Copilot 登录的只读用户权限。"""
        try:
            response = await self._request().post_res(
                self._DEVICE_CODE_URL,
                json={"client_id": client_id, "scope": scope},
                raise_exception=True,
            )
        except Exception as error:  # noqa: BLE001 - 网络异常统一转换为领域传输错误
            raise GithubAuthTransportError("无法连接 GitHub，请稍后重试。") from error
        payload = self._json_payload(response)
        self._raise_payload_error(payload, "申请 GitHub 设备码失败")
        try:
            return GithubDeviceCode(
                device_code=str(payload["device_code"]),
                user_code=str(payload["user_code"]),
                verification_uri=str(payload.get("verification_uri") or payload.get("verification_url") or ""),
                expires_in=max(int(payload.get("expires_in") or 900), 1),
                interval_seconds=max(int(payload.get("interval") or 5), 1),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GithubAuthTransportError("GitHub 返回的设备授权信息无效") from error

    async def exchange_device_code(
        self,
        client_id: str,
        device_code: str,
    ) -> GithubTokenExchange:
        """轮询 GitHub 设备码，并保留协议层授权状态。"""
        try:
            response = await self._request().post_res(
                self._ACCESS_TOKEN_URL,
                json={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                raise_exception=True,
            )
        except Exception as error:  # noqa: BLE001 - 网络异常统一转换为领域传输错误
            raise GithubAuthTransportError("无法连接 GitHub，请稍后重试。") from error
        return self._exchange_payload(self._json_payload(response))

    async def refresh_access_token(
        self,
        client_id: str,
        refresh_token: str,
    ) -> GithubTokenExchange:
        """使用 GitHub 返回的 refresh_token 申请新的访问 Token。"""
        try:
            response = await self._request().post_res(
                self._ACCESS_TOKEN_URL,
                json={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                raise_exception=True,
            )
        except Exception as error:  # noqa: BLE001 - 网络异常统一转换为领域传输错误
            raise GithubAuthTransportError("无法连接 GitHub，请稍后重试。") from error
        return self._exchange_payload(self._json_payload(response))

    async def get_user(self, access_token: str) -> GithubUser:
        """调用 GitHub 用户接口验证 Token 并读取登录名。"""
        try:
            response = await self._request(headers={"Authorization": f"Bearer {access_token}"}).get_res(
                self._USER_URL,
                raise_exception=True,
            )
        except Exception as error:  # noqa: BLE001 - 网络异常统一转换为领域传输错误
            raise GithubAuthTransportError("无法连接 GitHub，请稍后重试。") from error
        if response is None:
            raise GithubAuthTransportError("GitHub 未返回用户校验结果")
        if response.status_code == 401:
            raise GithubAuthTransportError("GitHub Token 已失效", unauthorized=True)
        if response.status_code >= 400:
            raise GithubAuthTransportError("GitHub Token 校验失败")
        payload = self._json_payload(response)
        login = str(payload.get("login") or "").strip()
        if not login:
            raise GithubAuthTransportError("GitHub 未返回用户信息")
        return GithubUser(login=login)

    def _request(self, *, headers: dict[str, str] | None = None) -> AsyncRequestUtils:
        """构造 GitHub OAuth 请求客户端，不复用当前业务 Token 请求头。"""
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": str(self._settings.get("USER_AGENT") or "MoviePilot"),
        }
        request_headers.update(headers or {})
        return AsyncRequestUtils(
            headers=request_headers,
            proxies=self._settings.get("PROXY"),
            timeout=20,
            verify=True,
            trust_env=False,
        )

    @staticmethod
    def _json_payload(response: Any) -> dict[str, Any]:
        """读取 GitHub JSON 响应，避免把原始响应文本写入业务错误。"""
        try:
            payload = response.json()
        except Exception as error:  # noqa: BLE001 - 外部 JSON 异常统一收敛
            raise GithubAuthTransportError("GitHub 返回了无效响应") from error
        if not isinstance(payload, dict):
            raise GithubAuthTransportError("GitHub 返回了无效响应")
        return payload

    @staticmethod
    def _raise_payload_error(payload: dict[str, Any], message: str) -> None:
        """将 GitHub OAuth 错误字段转换为不泄露响应原文的业务异常。"""
        if payload.get("error"):
            raise GithubAuthTransportError(message)

    @classmethod
    def _exchange_payload(cls, payload: dict[str, Any]) -> GithubTokenExchange:
        """把设备码或刷新响应转换为统一 Token 交换结果。"""
        return GithubTokenExchange(
            access_token=cls._optional_text(payload.get("access_token")),
            refresh_token=cls._optional_text(payload.get("refresh_token")),
            expires_in=cls._optional_int(payload.get("expires_in")),
            refresh_token_expires_in=cls._optional_int(payload.get("refresh_token_expires_in")),
            scope=cls._optional_text(payload.get("scope")),
            error=cls._optional_text(payload.get("error")),
        )

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        """规范化 GitHub 响应中的可选文本字段。"""
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        """规范化 GitHub 响应中的可选整数时间字段。"""
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
