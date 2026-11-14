"""
仪表板统计中未知数量、零数量和月度增量的契约。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.dashboard import DashboardQueryService
from app.application.history import TransferHistoryMonthlyStatistics
from app.schemas.dashboard import Statistic


@pytest.mark.parametrize(
    ("episode_counts", "expected_episodes"),
    [(None, 0), ([], 0), ([None], None), ([None, None], None), ([None, 0], 0), ([None, 3, 5], 8)],
)
def test_statistics_distinguishes_unknown_episodes_from_zero(episode_counts, expected_episodes):
    """
    缺少媒体服务、未知集数和已知零集数应保持不同返回语义。
    """
    rows = None if episode_counts is None else [
        Statistic(movie_count=2, tv_count=None, music_count=1, user_count=None, episode_count=count)
        for count in episode_counts
    ]
    provide_statistics = Mock(return_value=rows)
    monthly_statistics = Mock(return_value=TransferHistoryMonthlyStatistics(
        movies=10, tv_shows=20, episodes=30, music=40,
    ))
    service = DashboardQueryService(
        repository=SimpleNamespace(monthly_media_statistics=monthly_statistics),
        media_statistics=provide_statistics,
    )

    result = service.statistic(name="选定媒体库")

    assert result.episode_count == expected_episodes
    assert result.movie_count == 2 * len(rows or [])
    assert result.music_count == len(rows or [])
    assert result.tv_count == result.user_count == 0
    assert (result.movie_count_month, result.tv_count_month,
            result.episode_count_month, result.music_count_month) == (10, 20, 30, 40)
    assert rows is None or [row.episode_count for row in rows] == episode_counts
    provide_statistics.assert_called_once_with("选定媒体库")
    monthly_statistics.assert_called_once_with()
