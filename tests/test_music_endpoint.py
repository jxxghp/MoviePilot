import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from app.api.apiv1 import api_router
from app.api.endpoints.music import explore_music, recognize_music, search_music
from app.core.music import MusicInfo
from app.schemas.music import MusicRecognizeRequest


def test_music_routes_are_registered():
    """V1 API 应注册音乐搜索和详情识别路由。"""
    routes = {(route.path, tuple(route.methods or [])) for route in api_router.routes}

    assert any(path == "/music/search" and "GET" in methods for path, methods in routes)
    assert any(path == "/music/recognize" and "POST" in methods for path, methods in routes)
    assert any(path == "/music/explore" and "GET" in methods for path, methods in routes)


def test_search_music_serializes_chain_results():
    """音乐搜索接口应返回统一的 MusicInfo 响应。"""
    chain = Mock()
    chain.async_search = AsyncMock(
        return_value=[
            MusicInfo(
                source="musicbrainz",
                media_id="recording-1",
                title="晴天",
                artists=["周杰伦"],
            )
        ]
    )

    with patch("app.api.endpoints.music.MusicChain", return_value=chain):
        result = asyncio.run(search_music(query="晴天", count=10, _=Mock()))

    assert len(result) == 1
    assert result[0].title == "晴天"
    assert result[0].artist == "周杰伦"
    chain.async_search.assert_awaited_once_with(query="晴天", limit=10)


def test_recognize_music_returns_detail():
    """音乐识别接口应按来源和 ID 返回详情。"""
    chain = Mock()
    chain.async_recognize = AsyncMock(
        return_value=MusicInfo(
            source="musicbrainz",
            media_id="recording-1",
            title="晴天",
        )
    )

    with patch("app.api.endpoints.music.MusicChain", return_value=chain):
        result = asyncio.run(
            recognize_music(
                request=MusicRecognizeRequest(
                    source="musicbrainz",
                    media_id="recording-1",
                ),
                _=Mock(),
            )
        )

    assert result.media_id == "recording-1"
    chain.async_recognize.assert_awaited_once_with(
        source="musicbrainz",
        media_id="recording-1",
    )


def test_recognize_music_returns_404_for_unknown_item():
    """音乐详情不存在时接口应返回 404。"""
    chain = Mock()
    chain.async_recognize = AsyncMock(return_value=None)

    with (
        patch("app.api.endpoints.music.MusicChain", return_value=chain),
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(
            recognize_music(
                request=MusicRecognizeRequest(source="musicbrainz", media_id="missing"),
                _=Mock(),
            )
        )

    assert error.value.status_code == 404


def test_explore_music_forwards_filters_and_serializes_chart():
    """音乐探索接口应传递周期、排序、热度和封面筛选条件。"""
    chain = Mock()
    chain.async_chart = AsyncMock(
        return_value=[
            MusicInfo(
                source="musicbrainz",
                media_id="recording-1",
                title="晴天",
                artists=["周杰伦"],
                listen_count=123,
            )
        ]
    )

    with patch("app.api.endpoints.music.MusicChain", return_value=chain):
        result = asyncio.run(
            explore_music(
                page=2,
                count=20,
                range_name="this_week",
                sort_by="listen_count.asc",
                min_listen_count=100,
                with_cover=True,
                _=Mock(),
            )
        )

    assert result[0].listen_count == 123
    chain.async_chart.assert_awaited_once_with(
        range_name="this_week",
        page=2,
        count=20,
        sort_by="listen_count.asc",
        min_listen_count=100,
        with_cover=True,
    )
