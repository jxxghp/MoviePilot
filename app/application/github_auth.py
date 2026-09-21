"""GitHub Token 授权与持久化应用服务。"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.application.configuration import RuntimeSettingsService, SystemConfigService
from app.domain.github import (
    GithubAuthPort,
    GithubAuthTransportError,
    GithubTokenExchange,
    GithubUser,
)
from app.schemas.types import SystemConfigKey


class GithubAuthError(RuntimeError):
    """表示 GitHub 授权流程或本地 Token 持久化失败。"""

    def __init__(self, message: str, *, unauthorized: bool = False) -> None:
        """保存面向接口层的错误信息及是否为 Token 失效。"""
        super().__init__(message)
        self.unauthorized = unauthorized


@dataclass(slots=True)
class _PendingDeviceSession:
    """进程内设备授权会话，不保存任何用户 Token。"""

    device_code: str
    expires_at: float
    interval_seconds: int
    next_poll_at: float = 0.0


@dataclass(frozen=True, slots=True)
class GithubAuthStatus:
    """返回给 API 层的 GitHub Token 状态摘要。"""

    configured: bool
    valid: bool | None
    source: str | None
    login: str | None
    masked_token: str | None
    expires_at: int | None
    needs_reauthorization: bool


@dataclass(frozen=True, slots=True)
class GithubDeviceAuthStartResult:
    """设备授权启动后供前端展示的操作数据。"""

    session_id: str
    verification_uri: str
    user_code: str
    expires_in: int
    interval_seconds: int


@dataclass(frozen=True, slots=True)
class GithubDeviceAuthPollResult:
    """设备授权轮询的阶段结果。"""

    state: str
    message: str = ""
    retry_after: int | None = None
    status: GithubAuthStatus | None = None


class GithubAuthService:
    """编排 GitHub Device Flow、Token 刷新、状态校验和安全持久化。"""

    _AUTH_CONFIG_KEY = SystemConfigKey.GithubAuth
    _POLL_PENDING = "authorization_pending"
    _POLL_SLOW_DOWN = "slow_down"
    _POLL_DENIED = "access_denied"
    _POLL_EXPIRED = frozenset({"expired_token", "token_expired"})

    def __init__(
        self,
        *,
        settings: RuntimeSettingsService,
        system_config: SystemConfigService,
        transport: GithubAuthPort,
        client_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """注入部署设置、系统配置、GitHub 传输端口和可测试时钟。"""
        self._settings = settings
        self._system_config = system_config
        self._transport = transport
        self._client_id = client_id
        self._clock = clock
        self._lock = asyncio.Lock()
        self._pending_sessions: dict[str, _PendingDeviceSession] = {}

    async def start_device_auth(self) -> GithubDeviceAuthStartResult:
        """申请设备码并登记一个有时效的服务端轮询会话。"""
        try:
            device = await self._transport.request_device_code(self._client_id)
        except GithubAuthTransportError as error:
            raise GithubAuthError(str(error), unauthorized=error.unauthorized) from error
        if not device.device_code or not device.user_code or not device.verification_uri:
            raise GithubAuthError("GitHub 未返回完整的设备授权信息")

        now = self._clock()
        session_id = uuid4().hex
        async with self._lock:
            self._cleanup_sessions_locked(now)
            self._pending_sessions[session_id] = _PendingDeviceSession(
                device_code=device.device_code,
                expires_at=now + max(device.expires_in, 1),
                interval_seconds=max(device.interval_seconds, 1),
            )
        return GithubDeviceAuthStartResult(
            session_id=session_id,
            verification_uri=device.verification_uri,
            user_code=device.user_code,
            expires_in=max(device.expires_in, 1),
            interval_seconds=max(device.interval_seconds, 1),
        )

    async def poll_device_auth(self, session_id: str) -> GithubDeviceAuthPollResult:
        """按 GitHub 要求的频率轮询设备码，并在成功后保存 Token。"""
        now = self._clock()
        async with self._lock:
            session = self._pending_sessions.get(session_id)
            if session is None or session.expires_at <= now:
                self._pending_sessions.pop(session_id, None)
                return GithubDeviceAuthPollResult(
                    state="expired",
                    message="GitHub 设备码已过期，请重新开始授权。",
                )
            if session.next_poll_at > now:
                return GithubDeviceAuthPollResult(
                    state="pending",
                    message="等待用户在 GitHub 页面完成授权。",
                    retry_after=max(1, int(session.next_poll_at - now)),
                )
            session.next_poll_at = now + session.interval_seconds
            device_code = session.device_code

        try:
            exchange = await self._transport.exchange_device_code(
                self._client_id,
                device_code,
            )
        except GithubAuthTransportError as error:
            raise GithubAuthError(str(error), unauthorized=error.unauthorized) from error
        if exchange.access_token:
            try:
                github_user = await self._transport.get_user(exchange.access_token)
                await self._save_oauth_token(exchange, github_user)
            except GithubAuthError:
                raise
            except Exception as error:  # noqa: BLE001 - 外部响应仅转换为授权错误
                raise GithubAuthError("GitHub Token 校验失败，请稍后重试。") from error
            async with self._lock:
                self._pending_sessions.pop(session_id, None)
            return GithubDeviceAuthPollResult(
                state="authorized",
                message="GitHub Token 已设置。",
                status=await self.status(),
            )

        error_code = str(exchange.error or "").strip().lower()
        if error_code == self._POLL_PENDING:
            return GithubDeviceAuthPollResult(
                state="pending",
                message="等待用户在 GitHub 页面完成授权。",
                retry_after=await self._session_interval(session_id),
            )
        if error_code == self._POLL_SLOW_DOWN:
            interval = await self._increase_session_interval(session_id)
            return GithubDeviceAuthPollResult(
                state="slow_down",
                message="GitHub 要求降低轮询频率，稍后继续。",
                retry_after=interval,
            )
        if error_code == self._POLL_DENIED:
            await self._remove_session(session_id)
            return GithubDeviceAuthPollResult(
                state="denied",
                message="GitHub 授权已被拒绝。",
            )
        if error_code in self._POLL_EXPIRED:
            await self._remove_session(session_id)
            return GithubDeviceAuthPollResult(
                state="expired",
                message="GitHub 设备码已过期，请重新开始授权。",
            )
        if error_code:
            await self._remove_session(session_id)
            return GithubDeviceAuthPollResult(
                state="failed",
                message="GitHub 授权失败，请重新开始。",
            )
        return GithubDeviceAuthPollResult(
            state="pending",
            message="等待 GitHub 返回授权结果。",
            retry_after=await self._session_interval(session_id),
        )

    async def status(self) -> GithubAuthStatus:
        """返回脱敏状态，并在 OAuth Token 临近过期时尝试刷新。"""
        token = self._read_access_token()
        metadata = self._read_auth_metadata()
        if not token:
            return GithubAuthStatus(
                configured=False,
                valid=None,
                source=None,
                login=None,
                masked_token=None,
                expires_at=None,
                needs_reauthorization=False,
            )

        needs_reauthorization = False
        if metadata:
            token, metadata, needs_reauthorization = await self._refresh_if_needed(
                token,
                metadata,
            )
        login = str(metadata.get("login") or "").strip() or None
        valid: bool | None = None
        try:
            github_user = await self._transport.get_user(token)
            valid = True
            login = github_user.login or login
        except GithubAuthError as error:
            valid = False if error.unauthorized else None
            needs_reauthorization = needs_reauthorization or error.unauthorized
        except GithubAuthTransportError as error:
            valid = False if error.unauthorized else None
            needs_reauthorization = needs_reauthorization or error.unauthorized
        except Exception:  # noqa: BLE001 - 网络暂不可达不应让设置页误清 Token
            valid = None

        return GithubAuthStatus(
            configured=True,
            valid=valid,
            source="oauth" if metadata else "manual",
            login=login,
            masked_token=self._mask_token(token),
            expires_at=self._as_int(metadata.get("expires_at")),
            needs_reauthorization=needs_reauthorization,
        )

    async def set_manual_token(self, token: str) -> GithubAuthStatus:
        """保存手动 Token 并清理旧 OAuth 元数据，兼容原有 PAT 配置方式。"""
        normalized = token.strip()
        if not normalized:
            raise GithubAuthError("GitHub Token 不能为空")
        success, message = self._settings.update("GITHUB_TOKEN", normalized)
        if success is False:
            raise GithubAuthError(message or "GitHub Token 保存失败")
        await self.forget_oauth_metadata()
        return await self.status()

    async def clear_token(self) -> None:
        """清除 GitHub Token 和 OAuth 刷新元数据。"""
        success, message = self._settings.update("GITHUB_TOKEN", None)
        if success is False:
            raise GithubAuthError(message or "GitHub Token 清除失败")
        await self.forget_oauth_metadata()

    async def forget_oauth_metadata(self) -> None:
        """清除 OAuth 元数据但保留当前部署设置中的 Token 值。"""
        await self._system_config.async_delete(self._AUTH_CONFIG_KEY)

    async def _save_oauth_token(
        self,
        exchange: GithubTokenExchange,
        github_user: GithubUser,
    ) -> None:
        """安全写入访问 Token，并将刷新信息保存到受保护的系统配置。"""
        if not exchange.access_token:
            raise GithubAuthError("GitHub 未返回访问 Token")
        success, message = self._settings.update(
            "GITHUB_TOKEN",
            exchange.access_token,
        )
        if success is False:
            raise GithubAuthError(message or "GitHub Token 保存失败")
        metadata: dict[str, Any] = {
            "login": github_user.login,
            "scope": exchange.scope or "",
        }
        if exchange.refresh_token:
            metadata["refresh_token"] = exchange.refresh_token
        if exchange.expires_in is not None:
            metadata["expires_at"] = int(self._clock() + max(exchange.expires_in, 0))
        if exchange.refresh_token_expires_in is not None:
            metadata["refresh_token_expires_at"] = int(self._clock() + max(exchange.refresh_token_expires_in, 0))
        await self._system_config.async_set(self._AUTH_CONFIG_KEY, metadata)

    async def _refresh_if_needed(
        self,
        token: str,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any], bool]:
        """在过期窗口内刷新 OAuth Token，失败时保留原值供诊断和重试。"""
        expires_at = self._as_int(metadata.get("expires_at"))
        if expires_at is None or expires_at > int(self._clock()) + 60:
            return token, metadata, False
        refresh_token = str(metadata.get("refresh_token") or "").strip()
        if not refresh_token:
            return token, metadata, True
        try:
            exchange = await self._transport.refresh_access_token(
                self._client_id,
                refresh_token,
            )
        except (GithubAuthError, GithubAuthTransportError):
            return token, metadata, True
        if not exchange.access_token:
            return token, metadata, True
        refreshed_metadata = copy.deepcopy(metadata)
        if exchange.refresh_token:
            refreshed_metadata["refresh_token"] = exchange.refresh_token
        if exchange.expires_in is not None:
            refreshed_metadata["expires_at"] = int(self._clock() + max(exchange.expires_in, 0))
        if exchange.refresh_token_expires_in is not None:
            refreshed_metadata["refresh_token_expires_at"] = int(
                self._clock() + max(exchange.refresh_token_expires_in, 0)
            )
        success, _ = self._settings.update("GITHUB_TOKEN", exchange.access_token)
        if success is False:
            return token, metadata, True
        await self._system_config.async_set(
            self._AUTH_CONFIG_KEY,
            refreshed_metadata,
        )
        return exchange.access_token, refreshed_metadata, False

    async def _session_interval(self, session_id: str) -> int:
        """读取会话当前轮询间隔，并在会话刚过期时返回最小等待值。"""
        async with self._lock:
            session = self._pending_sessions.get(session_id)
            if session is None:
                return 1
            return max(session.interval_seconds, 1)

    async def _increase_session_interval(self, session_id: str) -> int:
        """处理 GitHub slow_down 响应并增加后续轮询间隔。"""
        async with self._lock:
            session = self._pending_sessions.get(session_id)
            if session is None:
                return 1
            session.interval_seconds = max(session.interval_seconds + 5, 10)
            session.next_poll_at = self._clock() + session.interval_seconds
            return session.interval_seconds

    async def _remove_session(self, session_id: str) -> None:
        """移除一个已经结束的设备授权会话。"""
        async with self._lock:
            self._pending_sessions.pop(session_id, None)

    def _cleanup_sessions_locked(self, now: float) -> None:
        """在设备码申请时清理已经过期的内存会话。"""
        for session_id, session in list(self._pending_sessions.items()):
            if session.expires_at <= now:
                del self._pending_sessions[session_id]

    def _read_access_token(self) -> str:
        """读取部署设置中的访问 Token 并去除空白。"""
        return str(self._settings.get("GITHUB_TOKEN") or "").strip()

    def _read_auth_metadata(self) -> dict[str, Any]:
        """读取 OAuth 元数据的深拷贝，避免修改配置缓存中的对象。"""
        value = self._system_config.get(self._AUTH_CONFIG_KEY)
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    @staticmethod
    def _mask_token(token: str) -> str:
        """生成不含可用凭据的 Token 摘要。"""
        if len(token) <= 8:
            return f"{token[:2]}…"
        return f"{token[:4]}…{token[-4:]}"

    @staticmethod
    def _as_int(value: Any) -> int | None:
        """把外部 OAuth 元数据中的时间字段规范化为整数。"""
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
