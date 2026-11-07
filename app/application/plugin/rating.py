"""
插件安装统计和评分应用用例。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from copy import deepcopy
from time import monotonic
from typing import Any

from cachetools import TTLCache

StatisticLoader = Callable[[], Awaitable[Mapping[str, Any]]]
RatingsLoader = Callable[[list[str] | None], Awaitable[Mapping[str, Any]]]
RatingLoader = Callable[[str], Awaitable[Mapping[str, Any]]]
RatingSubmitter = Callable[[str, float], Awaitable[Mapping[str, Any] | None]]


class PluginNotInstalledError(ValueError):
    """
    表示评分目标不在当前已安装插件集合中。
    """


class PluginRatingService:
    """
    通过注入的 MoviePilot Server 端口提供插件统计和评分用例。
    """

    def __init__(
        self,
        *,
        installed_plugins: Callable[[], Sequence[str]],
        statistic: StatisticLoader,
        ratings: RatingsLoader,
        rating: RatingLoader,
        submit: RatingSubmitter,
        cache_scope: Callable[[], Hashable] = lambda: None,
    ) -> None:
        """
        保存安装状态读取和远程评分窄端口，创建当前 lifespan 的有界缓存。

        :param cache_scope: 当前中心端、实例身份和统计开关组成的缓存隔离标识
        """
        self._installed_plugins = installed_plugins
        self._statistic = statistic
        self._ratings = ratings
        self._rating = rating
        self._submit = submit
        self._cache_scope = cache_scope
        # 仅由主事件循环访问；随 lifespan 释放，不把实例评分持久化到共享缓存。
        self._metrics: TTLCache[tuple[Hashable, str, tuple[str, ...] | None], Mapping[str, Any]] = TTLCache(
            maxsize=128, ttl=300, timer=monotonic
        )
        self._revision = 0

    async def _cached_query(
        self, kind: str, plugin_ids: tuple[str, ...] | None, loader: StatisticLoader
    ) -> Mapping[str, Any]:
        """
        复用五分钟内的指标结果，按中心端与实例身份隔离，失败空结果不缓存。

        评分提交期间发出的旧查询不得覆盖提交后的缓存；向调用方返回独立副本。
        """
        key = (self._cache_scope(), kind, plugin_ids)
        cached = self._metrics.get(key)
        if cached is not None:
            return deepcopy(cached)
        revision = self._revision
        result = await loader()
        if result and revision == self._revision:
            self._metrics[key] = deepcopy(result)
        return deepcopy(result)

    async def statistic(self) -> Mapping[str, Any]:
        """
        返回插件安装统计，复用当前实例最近五分钟的非空结果。
        """
        return await self._cached_query("statistic", None, self._statistic)

    async def ratings(self, plugin_ids: list[str] | None) -> Mapping[str, Any]:
        """
        批量返回插件评分快照，同一 ID 集合五分钟内不重复外呼。
        """
        ids = tuple(sorted(set(plugin_ids))) if plugin_ids is not None else None
        return await self._cached_query("ratings", ids, lambda: self._ratings(plugin_ids))

    async def rating(self, plugin_id: str) -> Mapping[str, Any]:
        """
        返回单个插件评分快照。
        """
        return await self._rating(plugin_id)

    async def submit(self, plugin_id: str, rating: float) -> Mapping[str, Any] | None:
        """
        校验安装状态后提交当前实例评分。
        """
        if plugin_id not in self._installed_plugins():
            raise PluginNotInstalledError(f"插件 {plugin_id} 未安装，无法评分")
        result = await self._submit(plugin_id, rating)
        if result is not None:
            self._revision += 1
            self._metrics.clear()
        return result


_rating_service: PluginRatingService | None = None


def configure_plugin_rating_service(service: PluginRatingService) -> None:
    """
    由启动组合根发布当前 lifespan 的插件评分服务。
    """
    global _rating_service
    _rating_service = service


def get_plugin_rating_service() -> PluginRatingService:
    """
    返回已经由启动组合根装配的插件评分服务。
    """
    if _rating_service is None:
        raise RuntimeError("插件评分服务尚未完成初始化")
    return _rating_service


def reset_plugin_rating_service() -> None:
    """
    清除当前 lifespan 的评分服务，供停机和隔离测试使用。
    """
    global _rating_service
    _rating_service = None
