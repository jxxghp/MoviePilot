"""
跨来源同名不同年份作品不得被缺集检查判定为已存在。
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.chain.download import DownloadChain
from app.db.models.mediaserver import MediaServerItem
from app.db.oper.mediaserver import MediaServerOper
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import ExistMediaInfo, MediaServerField
from app.schemas.types import MediaSource, MediaType


@pytest.fixture(autouse=True)
def _track(db):
    """
    把媒体服务器条目表纳入用例级回收。
    """
    db.watermark(MediaServerItem)


def _no_exists_info(db, year: str):
    """
    在媒体库已有同名 2024 年动画时，检查豆瓣真人剧第一季的缺集情况。
    """
    db.add(
        MediaServerItem(
            server="emby",
            library="lib",
            item_id="anime-2024",
            item_type="电视剧",
            title="同名剧集",
            year="2024",
            media_source=str(MediaSource.TMDB),
            media_id="259537",
            seasoninfo={1: list(range(1, 27))},
        )
    )
    chain = DownloadChain()
    chain.media_server_repository = MediaServerOper(db.session)

    def media_exists(mediainfo, itemid=None, server=None, *, query=None):
        """
        模拟媒体服务器按缓存条目返回动画已有的 26 集。
        """
        assert query.fields == {MediaServerField.IDENTITY, MediaServerField.YEAR}
        if itemid != "anime-2024":
            return None
        return ExistMediaInfo(
            type=MediaType.TV,
            server=server,
            itemid=itemid,
            seasons={1: list(range(1, 27))},
        )

    chain.media_exists = media_exists
    mediainfo = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="36073483",
        title="同名剧集",
        type=MediaType.TV,
        year=year,
        seasons={1: list(range(1, 25))},
    )
    with patch(
        "app.chain.download.existence.get_mediaserver_configs",
        return_value=[SimpleNamespace(name="emby")],
    ):
        return chain.get_no_exists_info(
            meta=MetaInfo("同名剧集"),
            mediainfo=mediainfo,
            totals={1: 24},
        )


def test_cross_source_same_title_other_year_is_not_complete(db):
    """
    豆瓣 2026 真人剧不得借用同名 2024 动画的集数而被判定完整。
    """
    exist_flag, no_exists = _no_exists_info(db, year="2026")

    assert exist_flag is False
    season = no_exists["douban:36073483"][1]
    assert season.total_episode == 24
    assert season.episodes == []


def test_cross_source_same_title_same_year_still_matches(db):
    """
    同年份的跨来源同名条目仍应配对，保留豆瓣订阅与 TMDB 媒体库的兼容。
    """
    exist_flag, no_exists = _no_exists_info(db, year="2024")

    assert exist_flag is True
    assert no_exists == {}
