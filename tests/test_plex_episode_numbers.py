from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

import pytest

from app.modules.plex.plex import Plex
from app.schemas.types import MediaSource


@pytest.mark.parametrize(
    ("numbers", "season", "expected"),
    [
        ([(None, 1), (1, None), (None, None), (1, 2)], None, {1: [2]}),
        ([(None, 1), (1, None), (1, 2), (2, 1)], 1, {1: [2]}),
        ([(None, 1), (1, None), (None, None)], None, {}),
        ([(0, 1), (1, 0), (1, 2)], None, {0: [1], 1: [0, 2]}),
        ([(0, 1), (1, 1)], 0, {0: [1]}),
    ],
    ids=["missing-numbers", "season-filter", "all-invalid", "preserve-zero", "specials"],
)
def test_plex_tv_episodes_skips_missing_numbers(
    numbers: list[tuple[Optional[int], Optional[int]]],
    season: Optional[int],
    expected: dict[int, list[int]],
) -> None:
    """缺失季集号不能进入同步结果，合法的零值和季过滤应保持不变。"""
    plex = Plex.__new__(Plex)
    plex._plex = Mock()
    show = Mock()
    show.key = "/library/metadata/123"
    show.guids = [{"id": "tmdb://12345"}]
    show.episodes.return_value = [
        SimpleNamespace(seasonNumber=season_number, index=episode_number)
        for season_number, episode_number in numbers
    ]
    plex._plex.fetchItem.return_value = show

    item_id, episodes = plex.get_tv_episodes(
        item_id="123",
        media_source=MediaSource.TMDB,
        media_id="12345",
        season=season,
    )

    assert item_id == show.key
    assert episodes == expected
    plex._plex.library.search.assert_not_called()
