"""MediaChain 跨来源身份投影的同步、异步对称性测试。"""

import asyncio
from typing import Any, Optional

from app.chain.media import MediaChain
from app.schemas.types import MediaSource, MediaType


class _SyncProjectionChain:
    """记录同步来源读取和目标匹配参数的投影测试桩。"""

    def __init__(self) -> None:
        """初始化固定来源详情和调用记录。"""
        self.tmdb_match: dict[str, Any] = {}
        self.douban_match: dict[str, Any] = {}
        self.tmdb_result = {"id": 550, "media_type": "tv"}

    @staticmethod
    def douban_info(
        doubanid: str,
        mtype: Optional[MediaType] = None,
    ) -> dict[str, Any]:
        """返回包含中英文标题和显式类型的豆瓣详情。"""
        assert doubanid == "1295644"
        assert mtype is None
        return {
            "id": doubanid,
            "title": "电视剧测试",
            "original_title": "Series Test",
            "year": "2026",
            "media_type": MediaType.TV,
        }

    def _match_tmdb_with_names(self, **kwargs: Any) -> dict[str, Any]:
        """记录 TMDB 匹配参数并返回可观察的原始详情。"""
        self.tmdb_match = kwargs
        return self.tmdb_result

    @staticmethod
    def tmdb_info(
        tmdbid: int,
        mtype: Optional[MediaType],
    ) -> dict[str, Any]:
        """返回含第零季年份和 IMDb 身份的 TMDB 详情。"""
        assert tmdbid == 550
        assert mtype == MediaType.TV
        return {
            "id": tmdbid,
            "name": "Series Test",
            "external_ids": {"imdb_id": "tt0000550"},
            "seasons": [
                {"season_number": 0, "air_date": "2025-12-01"},
                {"season_number": 1, "air_date": "2026-01-01"},
            ],
        }

    def match_doubaninfo(self, **kwargs: Any) -> dict[str, Any]:
        """记录豆瓣匹配参数并返回目标身份。"""
        self.douban_match = kwargs
        return {"id": "1295644", "media_type": "tv"}


class _AsyncProjectionChain:
    """记录异步来源读取和目标匹配参数的投影测试桩。"""

    def __init__(self) -> None:
        """初始化固定来源详情和调用记录。"""
        self.tmdb_match: dict[str, Any] = {}
        self.douban_match: dict[str, Any] = {}
        self.tmdb_result = {"id": 550, "media_type": "tv"}

    async def async_douban_info(
        self,
        doubanid: str,
        mtype: Optional[MediaType] = None,
    ) -> dict[str, Any]:
        """异步返回包含中英文标题和显式类型的豆瓣详情。"""
        return _SyncProjectionChain.douban_info(doubanid, mtype)

    async def _async_match_tmdb_with_names(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """记录异步 TMDB 匹配参数并返回原始详情。"""
        self.tmdb_match = kwargs
        return self.tmdb_result

    async def async_tmdb_info(
        self,
        tmdbid: int,
        mtype: Optional[MediaType],
    ) -> dict[str, Any]:
        """异步返回含第零季年份和 IMDb 身份的 TMDB 详情。"""
        return _SyncProjectionChain.tmdb_info(tmdbid, mtype)

    async def async_match_doubaninfo(self, **kwargs: Any) -> dict[str, Any]:
        """记录异步豆瓣匹配参数并返回目标身份。"""
        self.douban_match = kwargs
        return {"id": "1295644", "media_type": "tv"}


def test_douban_to_tmdb_keeps_result_fields_and_season_zero() -> None:
    """豆瓣转 TMDB 应保留目标详情字段并显式返回第零季。"""
    chain = _SyncProjectionChain()

    result = MediaChain.convert_media_identity(
        chain,
        target_source=MediaSource.TMDB,
        media_source=MediaSource.Douban,
        media_id="1295644",
        season=0,
    )

    assert result == {"id": 550, "media_type": "tv", "season": 0}
    assert chain.tmdb_result == {"id": 550, "media_type": "tv"}
    assert chain.tmdb_match == {
        "meta_names": ("Series Test", "测试"),
        "year": "2026",
        "mtype": MediaType.TV,
        "season": 0,
    }


def test_tmdb_to_douban_uses_season_zero_year_and_identity() -> None:
    """TMDB 转豆瓣应使用第零季年份并保留目标 id 和媒体类型。"""
    chain = _SyncProjectionChain()

    result = MediaChain.convert_media_identity(
        chain,
        target_source=MediaSource.Douban,
        media_source=MediaSource.TMDB,
        media_id="550",
        mtype=MediaType.TV,
        season=0,
    )

    assert result == {"id": "1295644", "media_type": "tv"}
    assert chain.douban_match == {
        "name": "Series Test",
        "year": "2025",
        "mtype": MediaType.TV,
        "imdbid": "tt0000550",
        "season": None,
    }


def test_async_douban_to_tmdb_matches_sync_projection() -> None:
    """异步豆瓣转 TMDB 应与同步规则保持字段和第零季语义一致。"""
    chain = _AsyncProjectionChain()

    result = asyncio.run(
        MediaChain.async_convert_media_identity(
            chain,
            target_source=MediaSource.TMDB,
            media_source=MediaSource.Douban,
            media_id="1295644",
            season=0,
        )
    )

    assert result == {"id": 550, "media_type": "tv", "season": 0}
    assert chain.tmdb_result == {"id": 550, "media_type": "tv"}
    assert chain.tmdb_match["season"] == 0
    assert chain.tmdb_match["mtype"] == MediaType.TV


def test_async_tmdb_to_douban_matches_sync_projection() -> None:
    """异步 TMDB 转豆瓣应复用同步纯规则生成第零季年份。"""
    chain = _AsyncProjectionChain()

    result = asyncio.run(
        MediaChain.async_convert_media_identity(
            chain,
            target_source=MediaSource.Douban,
            media_source=MediaSource.TMDB,
            media_id="550",
            mtype=MediaType.TV,
            season=0,
        )
    )

    assert result == {"id": "1295644", "media_type": "tv"}
    assert chain.douban_match["year"] == "2025"
    assert chain.douban_match["imdbid"] == "tt0000550"
