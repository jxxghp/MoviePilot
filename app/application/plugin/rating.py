"""插件安装统计和评分应用用例。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

StatisticLoader = Callable[[], Awaitable[Mapping[str, Any]]]
RatingsLoader = Callable[[list[str] | None], Awaitable[Mapping[str, Any]]]
RatingLoader = Callable[[str], Awaitable[Mapping[str, Any]]]
RatingSubmitter = Callable[[str, float], Awaitable[Mapping[str, Any] | None]]


class PluginNotInstalledError(ValueError):
    """表示评分目标不在当前已安装插件集合中。"""


class PluginRatingService:
    """通过注入的 MoviePilot Server 端口提供插件统计和评分用例。"""

    def __init__(
        self,
        *,
        installed_plugins: Callable[[], Sequence[str]],
        statistic: StatisticLoader,
        ratings: RatingsLoader,
        rating: RatingLoader,
        submit: RatingSubmitter,
    ) -> None:
        """保存安装状态读取和远程评分窄端口。"""
        self._installed_plugins = installed_plugins
        self._statistic = statistic
        self._ratings = ratings
        self._rating = rating
        self._submit = submit

    async def statistic(self) -> Mapping[str, Any]:
        """返回插件安装统计。"""
        return await self._statistic()

    async def ratings(
        self, plugin_ids: list[str] | None
    ) -> Mapping[str, Any]:
        """批量返回插件评分快照。"""
        return await self._ratings(plugin_ids)

    async def rating(self, plugin_id: str) -> Mapping[str, Any]:
        """返回单个插件评分快照。"""
        return await self._rating(plugin_id)

    async def submit(self, plugin_id: str, rating: float) -> Mapping[str, Any] | None:
        """校验安装状态后提交当前实例评分。"""
        if plugin_id not in self._installed_plugins():
            raise PluginNotInstalledError(f"插件 {plugin_id} 未安装，无法评分")
        return await self._submit(plugin_id, rating)


_rating_service: PluginRatingService | None = None


def configure_plugin_rating_service(service: PluginRatingService) -> None:
    """由启动组合根发布当前 lifespan 的插件评分服务。"""
    global _rating_service
    _rating_service = service


def get_plugin_rating_service() -> PluginRatingService:
    """返回已经由启动组合根装配的插件评分服务。"""
    if _rating_service is None:
        raise RuntimeError("插件评分服务尚未完成初始化")
    return _rating_service


def reset_plugin_rating_service() -> None:
    """清除当前 lifespan 的评分服务，供停机和隔离测试使用。"""
    global _rating_service
    _rating_service = None
