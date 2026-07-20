import asyncio
from typing import Optional

import pytest

from app.chain.media import MediaChain
from app.core.context import MediaInfo
from app.schemas.types import MediaType


@pytest.mark.parametrize(
    ("platform", "expected_type"),
    [
        ("剧场版", MediaType.MOVIE),
        ("Movie", MediaType.MOVIE),
        ("电影", MediaType.MOVIE),
        ("TV", MediaType.TV),
        ("WEB", MediaType.TV),
        ("OVA", MediaType.TV),
        ("未知", MediaType.TV),
        (None, MediaType.TV),
    ],
)
def test_bangumi_platform_maps_to_media_type(
        platform: Optional[str], expected_type: MediaType
) -> None:
    """Bangumi媒介平台应映射为正确的标准媒体类型。"""
    media_info = MediaInfo(bangumi_info={"id": 1, "name": "测试条目", "platform": platform})

    assert media_info.type == expected_type


def test_bangumi_media_type_does_not_override_explicit_type() -> None:
    """显式媒体类型应优先于Bangumi媒介平台。"""
    media_info = MediaInfo(
        type=MediaType.TV,
        bangumi_info={"id": 1, "name": "测试条目", "platform": "剧场版"},
    )

    assert media_info.type == MediaType.TV


class _SyncBangumiMediaChain:
    """同步Bangumi跨数据源转换测试桩。"""

    def __init__(self):
        """初始化调用参数记录。"""
        self.tmdb_mtype = None
        self.douban_mtype = None

    def bangumi_info(self, bangumiid: int) -> dict:
        """返回剧场版Bangumi条目信息。"""
        return {
            "id": bangumiid,
            "name": "Movie Test",
            "name_cn": "电影测试",
            "date": "2026-01-01",
            "platform": "剧场版",
        }

    @staticmethod
    def _extract_year_from_bangumi(bangumi_info: dict) -> str:
        """返回测试条目的年份。"""
        return bangumi_info["date"][:4]

    def _match_tmdb_with_names(self, **kwargs) -> dict:
        """记录TMDB匹配使用的媒体类型。"""
        self.tmdb_mtype = kwargs["mtype"]
        return {"id": 100}

    def match_doubaninfo(self, **kwargs) -> dict:
        """记录豆瓣匹配使用的媒体类型。"""
        self.douban_mtype = kwargs["mtype"]
        return {"id": "200"}


def test_bangumi_movie_conversion_uses_movie_type() -> None:
    """Bangumi剧场版转TMDB和豆瓣时均应按电影匹配。"""
    chain = _SyncBangumiMediaChain()

    tmdb_info = MediaChain.get_tmdbinfo_by_bangumiid(chain, 1)
    douban_info = MediaChain.get_doubaninfo_by_bangumiid(chain, 1)

    assert tmdb_info == {"id": 100}
    assert douban_info == {"id": "200"}
    assert chain.tmdb_mtype == MediaType.MOVIE
    assert chain.douban_mtype == MediaType.MOVIE


class _AsyncBangumiMediaChain:
    """异步Bangumi跨数据源转换测试桩。"""

    def __init__(self):
        """初始化调用参数记录。"""
        self.tmdb_mtype = None
        self.douban_mtype = None

    async def async_bangumi_info(self, bangumiid: int) -> dict:
        """返回剧场版Bangumi条目信息。"""
        return {
            "id": bangumiid,
            "name": "Movie Test",
            "name_cn": "电影测试",
            "date": "2026-01-01",
            "platform": "Movie",
        }

    @staticmethod
    def _extract_year_from_bangumi(bangumi_info: dict) -> str:
        """返回测试条目的年份。"""
        return bangumi_info["date"][:4]

    async def _async_match_tmdb_with_names(self, **kwargs) -> dict:
        """记录异步TMDB匹配使用的媒体类型。"""
        self.tmdb_mtype = kwargs["mtype"]
        return {"id": 100}

    async def async_match_doubaninfo(self, **kwargs) -> dict:
        """记录异步豆瓣匹配使用的媒体类型。"""
        self.douban_mtype = kwargs["mtype"]
        return {"id": "200"}


def test_async_bangumi_movie_conversion_uses_movie_type() -> None:
    """异步Bangumi电影转换应向TMDB和豆瓣传递电影类型。"""
    chain = _AsyncBangumiMediaChain()

    tmdb_info = asyncio.run(MediaChain.async_get_tmdbinfo_by_bangumiid(chain, 1))
    douban_info = asyncio.run(MediaChain.async_get_doubaninfo_by_bangumiid(chain, 1))

    assert tmdb_info == {"id": 100}
    assert douban_info == {"id": "200"}
    assert chain.tmdb_mtype == MediaType.MOVIE
    assert chain.douban_mtype == MediaType.MOVIE
