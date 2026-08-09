import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from app.api.apiv1 import api_router
from app.api.endpoints.music import (
    explore_music,
    music_album,
    music_artist,
    music_artist_albums,
    music_artist_related,
    recognize_music,
)
from app.api.endpoints import media as media_endpoints
from app.core.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo, MusicRelease
from app.schemas.music import MusicRecognizeRequest
from app.schemas.types import MediaType


def test_music_routes_are_registered():
    """V1 API 应注册音乐详情识别、探索及艺术家专辑浏览路由。"""
    routes = {(route.path, tuple(route.methods or [])) for route in api_router.routes}

    assert any(path == "/music/recognize" and "POST" in methods for path, methods in routes)
    assert any(path == "/music/explore" and "GET" in methods for path, methods in routes)
    assert any(path == "/music/album/{album_id}" and "GET" in methods for path, methods in routes)
    assert any(path == "/music/artist/{artist_id}" and "GET" in methods for path, methods in routes)
    assert any(
        path == "/music/artist/{artist_id}/albums" and "GET" in methods
        for path, methods in routes
    )
    assert any(
        path == "/music/artist/{artist_id}/related" and "GET" in methods
        for path, methods in routes
    )
    assert any(
        path == "/media/search" and "GET" in methods for path, methods in routes
    )


def test_media_search_routes_music_queries_with_query_kwarg():
    """统一媒体搜索的音乐分支应以 AsyncSearch 的关键字参数调用 MusicChain。"""
    from app.chain.music import MusicChain

    chain = Mock()
    chain.async_search = AsyncMock(
        return_value=[
            MusicInfo(
                source="musicbrainz",
                media_id="recording-1",
                music_type="recording",
                title="晴天",
                artists=["周杰伦"],
                release_date="2003-07-31",
                category="Album / Studio",
            )
        ]
    )

    with (
        patch("app.api.endpoints.media.MusicChain", return_value=chain) as music_chain,
        patch.object(media_endpoints, "MediaChain") as media_chain,
    ):
        result = asyncio.run(
            media_endpoints.search(
                title="晴天",
                type="music",
                count=30,
                page=1,
                _=Mock(),
            )
        )

    assert result[0]["media_id"] == "recording-1"
    assert result[0]["music_type"] == "recording"
    assert result[0]["title"] == "晴天"
    chain.async_search.assert_awaited_once_with(query="晴天", limit=30)
    media_chain.return_value.async_search.assert_not_called()


def test_recognize_music_returns_detail():
    """音乐识别接口应按来源和 ID 经统一识别入口返回详情。"""
    from app.chain.media import MediaChain

    chain = Mock()
    chain.async_recognize_media = AsyncMock(
        return_value=MusicInfo(
            source="musicbrainz",
            media_id="recording-1",
            title="晴天",
        )
    )

    with patch("app.api.endpoints.music.MediaChain", return_value=chain):
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
    chain.async_recognize_media.assert_awaited_once_with(
        source="musicbrainz",
        mediaid="recording-1",
        mtype=MediaType.MUSIC,
    )


def test_recognize_music_returns_404_for_unknown_item():
    """音乐详情不存在时接口应返回 404。"""
    from app.chain.media import MediaChain

    chain = Mock()
    chain.async_recognize_media = AsyncMock(return_value=None)

    with (
        patch("app.api.endpoints.music.MediaChain", return_value=chain),
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
    """音乐探索接口应传递实体、周期、排序、热度和封面筛选条件。"""
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
        entity="recording",
    )


def test_explore_music_supports_official_fresh_release_mode():
    """新发行模式应按 ListenBrainz 官方排序和时间窗口请求探索数据。"""
    chain = Mock()
    chain.async_fresh_releases = AsyncMock(
        return_value=[
            MusicInfo(
                source="musicbrainz",
                media_id="release-group-1",
                music_type="album",
                title="ARIRANG",
                artists=["BTS"],
            )
        ]
    )

    with patch("app.api.endpoints.music.MusicChain", return_value=chain):
        result = asyncio.run(
            explore_music(
                page=1,
                count=30,
                mode="fresh",
                sort="artist_credit_name",
                days=30,
                past=True,
                future=False,
                with_cover=True,
                _=Mock(),
            )
        )

    assert result[0].music_type == "album"
    chain.async_fresh_releases.assert_awaited_once_with(
        days=30,
        sort="artist_credit_name",
        past=True,
        future=False,
        page=1,
        count=30,
        with_cover=True,
    )


def test_music_album_returns_tracks_and_releases():
    """专辑接口应返回专辑详情、曲目和发行版本。"""
    chain = Mock()
    chain.async_album = AsyncMock(
        return_value=MusicAlbumInfo(
            source="musicbrainz",
            media_id="release-group-1",
            title="A Night at the Opera",
            artists=["Queen"],
            artist_ids=["artist-1"],
            album_type="Album",
            release_date="1975-11-21",
            tracks=[MusicInfo(source="musicbrainz", media_id="recording-1", title="Love of My Life")],
            releases=[MusicRelease(media_id="release-1", title="A Night at the Opera", date="1975")],
        )
    )

    with patch("app.api.endpoints.music.MusicChain", return_value=chain):
        result = asyncio.run(music_album(album_id="release-group-1", _=Mock()))

    assert result.music_type == "album"
    assert result.year == 1975
    assert result.total_tracks == 1
    assert result.tracks[0].media_id == "recording-1"
    assert result.releases[0].media_id == "release-1"
    chain.async_album.assert_awaited_once_with(source="musicbrainz", media_id="release-group-1")


def test_music_album_returns_404_for_unknown_album():
    """专辑不存在时接口应返回 404。"""
    chain = Mock()
    chain.async_album = AsyncMock(return_value=None)

    with (
        patch("app.api.endpoints.music.MusicChain", return_value=chain),
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(music_album(album_id="missing", _=Mock()))

    assert error.value.status_code == 404


def test_music_artist_returns_detail():
    """艺术家接口应返回名称、类型和活跃时间。"""
    chain = Mock()
    chain.async_artist = AsyncMock(
        return_value=MusicArtistInfo(
            source="musicbrainz",
            media_id="artist-1",
            name="Queen",
            artist_type="Group",
            begin_date="1970-06-27",
        )
    )

    with patch("app.api.endpoints.music.MusicChain", return_value=chain):
        result = asyncio.run(music_artist(artist_id="artist-1", _=Mock()))

    assert result.name == "Queen"
    assert result.title == "Queen"
    assert result.music_type == "artist"
    chain.async_artist.assert_awaited_once_with(source="musicbrainz", media_id="artist-1")


def test_music_artist_albums_forwards_pagination_and_type():
    """艺术家专辑接口应传递分页和专辑类型筛选。"""
    chain = Mock()
    chain.async_artist_albums = AsyncMock(
        return_value=[
            MusicInfo(
                source="musicbrainz",
                media_id="release-group-1",
                music_type="album",
                title="News of the World",
            )
        ]
    )

    with patch("app.api.endpoints.music.MusicChain", return_value=chain):
        result = asyncio.run(
            music_artist_albums(artist_id="artist-1", page=2, count=10, album_type="ep", _=Mock())
        )

    assert result[0].media_id == "release-group-1"
    chain.async_artist_albums.assert_awaited_once_with(
        source="musicbrainz",
        media_id="artist-1",
        page=2,
        count=10,
        album_type="ep",
    )


def test_music_artist_related_returns_relationship_text():
    """关联艺术家接口应返回关系说明，供详情页展示。"""
    chain = Mock()
    chain.async_artist_related = AsyncMock(
        return_value=[
            MusicArtistInfo(
                source="musicbrainz",
                media_id="artist-2",
                name="Freddie Mercury",
                relation="member of band",
            )
        ]
    )

    with patch("app.api.endpoints.music.MusicChain", return_value=chain):
        result = asyncio.run(music_artist_related(artist_id="artist-1", count=5, _=Mock()))

    assert result[0].relation == "member of band"
    chain.async_artist_related.assert_awaited_once_with(
        source="musicbrainz",
        media_id="artist-1",
        count=5,
    )
