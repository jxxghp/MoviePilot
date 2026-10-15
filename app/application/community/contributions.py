"""社区贡献认定、身份隔离缓存与贡献等级编排。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from app.domain.github import (
    COMMUNITY_REPOSITORIES,
    GithubAuthTransportError,
    GithubCommunityPort,
    GithubCommunityProfile,
    GithubRepositoryContribution,
    GithubUser,
)


class CommunityContributionService:
    """按实例已验证 GitHub 身份核对社区贡献，不向普通本地用户继承荣誉。"""

    def __init__(
        self, *, token_reader: Callable[[], Awaitable[str]], transport: GithubCommunityPort,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """注入服务端凭据与传输端口；缓存只保存摘要键和公开贡献事实。"""
        self._token_reader = token_reader
        self._transport = transport
        self._clock = clock
        self._lock = asyncio.Lock()
        self._key = ""
        self._profile = GithubCommunityProfile()
        self._attempted_at = float("-inf")

    async def profile(self, *, refresh: bool = False) -> GithubCommunityProfile:
        """合并并发查询，缓存六小时；主动刷新最短间隔一分钟，失败不冒充零贡献。"""
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
            if elapsed < 60 or (not refresh and elapsed < 21600 and not self._profile.stale
                                and self._profile.state == "ready"):
                return self._profile
            self._attempted_at = now
            try:
                async with asyncio.timeout(45):
                    user = await self._transport.get_user(token)
                    if user.user_id <= 0:
                        raise GithubAuthTransportError("GitHub 身份无效")
                    repositories = await self._collect(token, user)
                # Token 在查询期间切换时，旧账号的异步结果不能重新点亮入口。
                if await self._token_reader() != token:
                    self._key = ""
                    self._profile = GithubCommunityProfile()
                    return self._profile
                self._profile = self._recognize(user.login, user.user_id, repositories, now)
            except (GithubAuthTransportError, TimeoutError) as error:
                if isinstance(error, GithubAuthTransportError) and error.unauthorized:
                    self._profile = GithubCommunityProfile(state="unavailable")
                else:
                    self._profile = replace(self._profile, state=(
                        "ready" if self._profile.checked_at is not None else "unavailable"
                    ), stale=True)
            return self._profile

    async def _collect(self, token: str, user: GithubUser) -> tuple[GithubRepositoryContribution, ...]:
        """逐仓查询避免同时突发搜索请求，任一失败不发布不完整的等级快照。"""
        results = []
        for repository, area, branch in COMMUNITY_REPOSITORIES:
            item = await self._transport.get_repository_contribution(token, user, repository, area, branch)
            if item.units:
                results.append(item)
        return tuple(results)

    @staticmethod
    def _recognize(
        login: str, user_id: int, repositories: tuple[GithubRepositoryContribution, ...], now: float,
    ) -> GithubCommunityProfile:
        """等级按同仓去重后的贡献量认定；核心是里程碑称号，不授予仓库权限。"""
        total = sum(item.units for item in repositories)
        areas = {item.area for item in repositories}
        tier = next((name for minimum, name in (
            (200, "core"), (50, "gold"), (10, "silver"), (1, "bronze"),
        ) if total >= minimum), None)
        specialty = next(iter(areas)) if len(areas) == 1 else ("ecosystem" if areas else None)
        return GithubCommunityProfile(
            state="ready", login=login, user_id=user_id, tier=tier, specialty=specialty,
            total=total, repositories=repositories, checked_at=int(now),
        )
