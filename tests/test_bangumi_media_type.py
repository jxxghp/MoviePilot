import asyncio
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.application.orchestration.media import MediaChain
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource, MediaType


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

    tmdb_info = MediaChain.convert_media_identity(
        chain,
        target_source=MediaSource.TMDB,
        media_source=MediaSource.Bangumi,
        media_id="1",
    )
    douban_info = MediaChain.convert_media_identity(
        chain,
        target_source=MediaSource.Douban,
        media_source=MediaSource.Bangumi,
        media_id="1",
    )

    assert tmdb_info == {"id": 100}
    assert douban_info == {"id": "200"}
    assert chain.tmdb_mtype == MediaType.MOVIE
    assert chain.douban_mtype == MediaType.MOVIE


def test_media_identity_conversion_rejects_invalid_pair_without_plugin_handler() -> None:
    """跨源转换拒绝无效 pair，且没有插件处理器时返回空结果。"""
    chain = _SyncBangumiMediaChain()

    assert MediaChain.convert_media_identity(
        chain,
        target_source=MediaSource.TMDB,
        media_source=MediaSource.Bangumi,
        media_id="0",
    ) is None
    with patch("app.application.orchestration.media.eventmanager.send_event", return_value=None):
        assert MediaChain.convert_media_identity(
            chain,
            target_source=MediaSource.TheAudioDB,
            media_source=MediaSource.Bangumi,
            media_id="1",
        ) is None
    assert chain.tmdb_mtype is None
    assert chain.douban_mtype is None


def test_media_identity_conversion_dispatches_plugin_source() -> None:
    """内置转换无匹配时应把动态来源交给插件转换事件。"""
    chain = _SyncBangumiMediaChain()
    result = {"media_source": MediaSource.TMDB, "media_id": "550"}

    def handle_event(_event_type, event_data):
        """模拟插件在链式事件中写入转换结果。"""
        event_data.media_dict.update(result)
        return Mock(event_data=event_data)

    with patch("app.application.orchestration.media.eventmanager.send_event", side_effect=handle_event):
        converted = MediaChain.convert_media_identity(
            chain,
            target_source=MediaSource.TMDB,
            media_source=MediaSource("acme.video"),
            media_id="custom-1",
        )

    assert converted == result


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

    tmdb_info = asyncio.run(
        MediaChain.async_convert_media_identity(
            chain,
            target_source=MediaSource.TMDB,
            media_source=MediaSource.Bangumi,
            media_id="1",
        )
    )
    douban_info = asyncio.run(
        MediaChain.async_convert_media_identity(
            chain,
            target_source=MediaSource.Douban,
            media_source=MediaSource.Bangumi,
            media_id="1",
        )
    )

    assert tmdb_info == {"id": 100}
    assert douban_info == {"id": "200"}
    assert chain.tmdb_mtype == MediaType.MOVIE
    assert chain.douban_mtype == MediaType.MOVIE


def test_async_media_identity_conversion_dispatches_plugin_source() -> None:
    """异步内置转换无匹配时也应分派插件转换事件。"""
    chain = _AsyncBangumiMediaChain()
    result = {"media_source": MediaSource.Douban, "media_id": "1295644"}

    async def handle_event(_event_type, event_data):
        """模拟异步插件在链式事件中写入转换结果。"""
        event_data.media_dict.update(result)
        return Mock(event_data=event_data)

    with patch(
        "app.application.orchestration.media.eventmanager.async_send_event",
        new=AsyncMock(side_effect=handle_event),
    ):
        converted = asyncio.run(MediaChain.async_convert_media_identity(
            chain,
            target_source=MediaSource.Douban,
            media_source=MediaSource("acme.video"),
            media_id="custom-1",
        ))

    assert converted == result
