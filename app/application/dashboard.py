"""
Dashboard 统计查询用例。
"""

from collections.abc import Callable
from typing import Optional, Protocol

from app.application.history import (
    TransferHistoryMonthlyStatistics,
    TransferHistoryStatisticSnapshot,
)
from app.schemas.dashboard import Statistic


class TransferHistoryQueryRepository(Protocol):
    """
    Dashboard 所需的整理历史统计端口。
    """

    def monthly_media_statistics(self) -> TransferHistoryMonthlyStatistics:
        """
        返回本月电影、剧集、单集和音乐数量。

        :return: 本月按媒体类型与剧集维度汇总的新增数量
        """
        ...

    async def async_statistic(
            self,
            days: int = 7,
    ) -> list[TransferHistoryStatisticSnapshot]:
        """
        返回最近若干天的整理趋势。

        :param days: 查询最近的自然日数量，默认 7 天
        :return: 按日期汇总的整理数量与大小等趋势记录
        """
        ...


class DashboardQueryService:
    """
    汇总媒体服务数据与整理历史统计。
    """

    def __init__(
            self,
            *,
            repository: TransferHistoryQueryRepository,
            media_statistics: Callable[[Optional[str]], Optional[list[Statistic]]],
    ) -> None:
        """
        保存整理历史端口和媒体服务统计提供方。
        """
        self._repository = repository
        self._media_statistics = media_statistics

    def statistic(self, name: Optional[str] = None) -> Statistic:
        """
        返回媒体服务总量和本月新增量。
        """
        media_statistics = self._media_statistics(name)
        if media_statistics:
            movie_count = tv_count = music_count = user_count = episode_count = 0
            has_episode_count = False
            for item in media_statistics:
                movie_count += item.movie_count or 0
                tv_count += item.tv_count or 0
                music_count += item.music_count or 0
                user_count += item.user_count or 0
                if item.episode_count is not None:
                    episode_count += item.episode_count or 0
                    has_episode_count = True
            result = Statistic(
                movie_count=movie_count,
                tv_count=tv_count,
                music_count=music_count,
                user_count=user_count,
                episode_count=episode_count if has_episode_count else None,
            )
        else:
            result = Statistic()

        monthly = self._repository.monthly_media_statistics()
        result.movie_count_month = monthly.movies
        result.tv_count_month = monthly.tv_shows
        result.episode_count_month = monthly.episodes
        result.music_count_month = monthly.music
        return result

    async def transfer(self, days: int = 7) -> list[int]:
        """
        返回最近若干天的整理数量序列。
        """
        rows = await self._repository.async_statistic(days)
        return [row.count for row in rows]
