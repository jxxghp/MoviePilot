"""
社区勋章查询：核验实例 GitHub 身份、读取 Wiki 名单并按凭据隔离缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from app.domain.github import (
    GithubAuthTransportError,
    GithubCommunityPort,
    GithubCommunityProfile,
)

# 名单由 Wiki 定时任务更新，实例侧缓存六小时；主动刷新也至少间隔一分钟，避免放大外部请求。
_CACHE_SECONDS = 21600
_REFRESH_COOLDOWN_SECONDS = 60
_QUERY_TIMEOUT_SECONDS = 45


class CommunityMedalService:
    """
    按实例 Token 对应的 GitHub 数字 ID 查询社区勋章，不向普通本地用户继承荣誉。

    计算与人工颁发都在 Wiki 名单完成，实例只核验身份并查表；身份只认 ``GET /user``
    返回的数字 ID，不接受前端或参数声明的用户名。功能是否开放由本机设置与 MoviePilot
    Server 远程开关共同决定，关闭时不核验身份也不拉取名单。
    """

    def __init__(
        self, *, token_reader: Callable[[], Awaitable[str]], transport: GithubCommunityPort,
        enabled_reader: Callable[[], Awaitable[bool]], clock: Callable[[], float] = time.time,
    ) -> None:
        """
        注入服务端凭据、传输端口与远程开关；缓存只保存 Token 摘要键和公开名单记录。

        ``enabled_reader`` 读取本机与服务端合成的社区勋章有效开关，取不到时必须返回 False；
        未配置 Token 时它应按服务端全局开关作答，使已开放的功能返回 ``unconfigured``。
        """
        self._token_reader = token_reader
        self._enabled_reader = enabled_reader
        self._transport = transport
        self._clock = clock
        self._lock = asyncio.Lock()
        self._key = ""
        self._profile = GithubCommunityProfile()
        self._attempted_at = float("-inf")

    async def profile(self, *, refresh: bool = False) -> GithubCommunityProfile:
        """
        合并并发查询并按凭据缓存；名单拉取失败保留同一 Token 的上次结果并标记过期。

        开关关闭时直接返回 ``disabled``，不触发任何 GitHub 请求，也不改动已有缓存，
        以便开关重新打开后沿用冷却与缓存策略。
        """
        if not await self._enabled_reader():
            return GithubCommunityProfile(state="disabled")
        async with self._lock:
            token = await self._token_reader()
            key = hashlib.sha256(token.encode()).hexdigest() if token else ""
            if key != self._key or not token:
                self._key = key
                self._profile = GithubCommunityProfile()
                self._attempted_at = float("-inf")
            if not token:
                return self._profile
            now = self._clock()
            elapsed = now - self._attempted_at
            if elapsed < _REFRESH_COOLDOWN_SECONDS or (
                not refresh and elapsed < _CACHE_SECONDS and not self._profile.stale
                and self._profile.state == "ready"
            ):
                return self._profile
            self._attempted_at = now
            try:
                async with asyncio.timeout(_QUERY_TIMEOUT_SECONDS):
                    user = await self._transport.get_user(token)
                    if user.user_id <= 0:
                        raise GithubAuthTransportError("GitHub 身份无效")
                    snapshot = await self._transport.get_medal_snapshot(user.user_id)
                # Token 在查询期间切换时，旧账号的异步结果不能重新点亮入口。
                if await self._token_reader() != token:
                    self._key = ""
                    self._profile = GithubCommunityProfile()
                    return self._profile
                self._profile = GithubCommunityProfile(
                    state="ready", login=user.login, user_id=user.user_id, medals=snapshot.medals,
                    generated_at=snapshot.generated_at, checked_at=int(now),
                )
            except (GithubAuthTransportError, TimeoutError) as error:
                if isinstance(error, GithubAuthTransportError) and error.unauthorized:
                    self._profile = GithubCommunityProfile(state="unavailable")
                else:
                    self._profile = replace(self._profile, state=(
                        "ready" if self._profile.checked_at is not None else "unavailable"
                    ), stale=True)
            return self._profile
