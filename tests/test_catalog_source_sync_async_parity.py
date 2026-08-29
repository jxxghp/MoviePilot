"""目录来源 Module 同步/异步决策一致性测试。"""

import asyncio
from copy import deepcopy
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.context import MediaInfo, MusicAlbumInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.modules.anilist import AniListModule
from app.modules.bangumi import BangumiModule
from app.modules.douban import DoubanModule
from app.modules.imdb import ImdbModule
from app.modules.imdb.api import ImdbAka, ImdbApi, ImdbTitle
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.theaudiodb import TheAudioDbModule
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
)


def _meta(
    name: str, *, media_type: MediaType = MediaType.TV, year: str = "2023"
) -> MetaBase:
    """构造带稳定标题、类型、年份和季号的识别元数据。"""
    meta = MetaBase(name)
    meta.name = name
    meta.cn_name = name
    meta.type = media_type
    meta.year = year
    meta.begin_season = 2
    return meta


def _signature(media: Optional[MediaInfo]) -> Optional[tuple[Any, ...]]:
    """提取同步和异步结果必须一致的统一媒体字段。"""
    if media is None:
        return None
    return (
        media.media_source,
        media.media_id,
        media.type,
        media.title,
        media.year,
        media.season,
    )


def _music_signature(music: Optional[MusicInfo]) -> Optional[tuple[Any, ...]]:
    """提取音乐识别双 ABI 必须一致的来源、身份和展示字段。"""
    if music is None:
        return None
    return (
        music.media_source,
        music.media_id,
        music.music_type,
        music.title,
        tuple(music.artists),
        music.album,
        music.album_id,
        music.track_number,
    )


def _anilist_info() -> dict:
    """构造可覆盖识别与搜索投影的 AniList 详情。"""
    return {
        "id": 154587,
        "title": {
            "romaji": "Sousou no Frieren",
            "native": "葬送のフリーレン",
            "chinese": "葬送的芙莉莲",
        },
        "format": "TV",
        "startDate": {"year": 2023},
        "episodes": 28,
        "characters": {"edges": []},
        "staff": {"edges": []},
    }


@pytest.mark.parametrize(
    ("media_source", "media_id", "meta", "mtype", "expected"),
    [
        (MediaSource.AniList, "154587", None, None, True),
        (MediaSource.AniList, None, _meta("Frieren"), None, True),
        (MediaSource.Douban, "154587", None, None, False),
        (MediaSource.AniList, "invalid", None, None, False),
        (MediaSource.AniList, None, _meta("Music", media_type=MediaType.MUSIC), None, False),
    ],
)
def test_anilist_recognition_sync_async_parity(
    media_source: MediaSource,
    media_id: Optional[str],
    meta: Optional[MetaBase],
    mtype: Optional[MediaType],
    expected: bool,
) -> None:
    """AniList 来源准入、ID 校验和结果投影在双 ABI 下应一致。"""
    info = _anilist_info()
    module = AniListModule()
    module.anilist_api = Mock()
    module.anilist_api.detail.return_value = deepcopy(info)
    module.anilist_api.search.return_value = [deepcopy(info)]
    module.anilist_api.async_detail = AsyncMock(return_value=deepcopy(info))
    module.anilist_api.async_search = AsyncMock(return_value=[deepcopy(info)])

    sync_result = module.recognize_media(
        meta=meta, media_source=media_source, media_id=media_id, mtype=mtype
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=meta, media_source=media_source, media_id=media_id, mtype=mtype
        )
    )

    assert bool(sync_result) is expected
    assert _signature(sync_result) == _signature(async_result)


def _bangumi_info() -> dict:
    """构造可覆盖识别与搜索投影的 Bangumi 详情。"""
    return {
        "id": 400602,
        "name": "Sousou no Frieren",
        "name_cn": "葬送的芙莉莲",
        "platform": "TV",
        "date": "2023-09-29",
        "eps": 28,
    }


@pytest.mark.parametrize(
    ("media_source", "media_id", "meta", "mtype", "expected"),
    [
        (MediaSource.Bangumi, "400602", None, None, True),
        (MediaSource.Bangumi, None, _meta("葬送的芙莉莲"), None, True),
        (MediaSource.TMDB, "400602", None, None, False),
        (MediaSource.Bangumi, "invalid", None, None, False),
        (MediaSource.Bangumi, None, _meta("Music", media_type=MediaType.MUSIC), None, False),
    ],
)
def test_bangumi_recognition_sync_async_parity(
    media_source: MediaSource,
    media_id: Optional[str],
    meta: Optional[MetaBase],
    mtype: Optional[MediaType],
    expected: bool,
) -> None:
    """Bangumi 来源准入、候选选择和结果投影在双 ABI 下应一致。"""
    info = _bangumi_info()
    candidate = {"id": 400602, "name": info["name"], "name_cn": info["name_cn"]}
    actors = [{"name": "种崎敦美"}]
    module = BangumiModule()
    module.bangumiapi = Mock()
    module.bangumiapi.search.return_value = [candidate]
    module.bangumiapi.detail.return_value = deepcopy(info)
    module.bangumiapi.credits.return_value = actors
    module.bangumiapi.async_search = AsyncMock(return_value=[candidate])
    module.bangumiapi.async_detail = AsyncMock(return_value=deepcopy(info))
    module.bangumiapi.async_credits = AsyncMock(return_value=actors)

    sync_result = module.recognize_media(
        meta=meta, media_source=media_source, media_id=media_id, mtype=mtype
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=meta, media_source=media_source, media_id=media_id, mtype=mtype
        )
    )

    assert bool(sync_result) is expected
    assert _signature(sync_result) == _signature(async_result)


def _imdb_title(primary_title: str = "Breaking Bad") -> ImdbTitle:
    """构造无需外部请求的 IMDb 电视剧候选。"""
    return ImdbTitle(
        id="tt0903747",
        type="tvSeries",
        primaryTitle=primary_title,
        originalTitle=primary_title,
        startYear=2008,
    )


def _imdb_module(title: ImdbTitle, alias: str = "绝命毒师") -> ImdbModule:
    """构造同步和异步客户端数据完全一致的 IMDb Module。"""
    api = Mock(spec=ImdbApi)
    api.search_titles.return_value = [title]
    api.get_title.return_value = title
    api.list_akas.return_value = [ImdbAka(text=alias)]
    api.list_credits.return_value = []
    api.list_images.return_value = []
    api.list_episodes.return_value = []
    api.list_seasons.return_value = []
    api.async_search_titles = AsyncMock(return_value=[title])
    api.async_get_title = AsyncMock(return_value=title)
    api.async_list_akas = AsyncMock(return_value=[ImdbAka(text=alias)])
    api.async_list_credits = AsyncMock(return_value=[])
    api.async_list_images = AsyncMock(return_value=[])
    api.async_list_episodes = AsyncMock(return_value=[])
    api.async_list_seasons = AsyncMock(return_value=[])
    module = ImdbModule()
    module.imdb_api = api
    return module


@pytest.mark.parametrize(
    ("media_source", "media_id", "meta", "mtype", "expected"),
    [
        (MediaSource.IMDb, "TT0903747", None, None, True),
        (MediaSource.IMDb, None, _meta("绝命毒师", year="2008"), None, True),
        (MediaSource.TMDB, "tt0903747", None, None, False),
        (MediaSource.IMDb, "0903747", None, None, False),
        (MediaSource.IMDb, None, _meta("Music", media_type=MediaType.MUSIC), None, False),
    ],
)
def test_imdb_recognition_sync_async_parity(
    media_source: MediaSource,
    media_id: Optional[str],
    meta: Optional[MetaBase],
    mtype: Optional[MediaType],
    expected: bool,
) -> None:
    """IMDb 来源准入、别名候选和结果收尾在双 ABI 下应一致。"""
    module = _imdb_module(_imdb_title(primary_title="Original Title"))

    sync_result = module.recognize_media(
        meta=meta, media_source=media_source, media_id=media_id, mtype=mtype
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=meta, media_source=media_source, media_id=media_id, mtype=mtype
        )
    )

    assert bool(sync_result) is expected
    assert _signature(sync_result) == _signature(async_result)


@pytest.mark.parametrize(
    ("source", "selected_source", "expected_none"),
    [
        (MediaSource.AniList, MediaSource.AniList, False),
        (MediaSource.AniList, MediaSource.TMDB, True),
        (MediaSource.Bangumi, MediaSource.Bangumi, False),
        (MediaSource.Bangumi, MediaSource.TMDB, True),
        (MediaSource.IMDb, MediaSource.IMDb, False),
        (MediaSource.IMDb, MediaSource.TMDB, True),
    ],
)
def test_catalog_search_sync_async_projection_parity(
    source: MediaSource,
    selected_source: MediaSource,
    expected_none: bool,
) -> None:
    """三个目录来源的搜索过滤、排序和投影结果在双 ABI 下应一致。"""
    if source == MediaSource.AniList:
        info = _anilist_info()
        module = AniListModule()
        module.anilist_api = Mock()
        module.anilist_api.search.return_value = [deepcopy(info)]
        module.anilist_api.async_search = AsyncMock(return_value=[deepcopy(info)])
        meta = _meta("Frieren")
    elif source == MediaSource.Bangumi:
        info = _bangumi_info()
        module = BangumiModule()
        module.bangumiapi = Mock()
        module.bangumiapi.search.return_value = [deepcopy(info)]
        module.bangumiapi.async_search = AsyncMock(return_value=[deepcopy(info)])
        meta = _meta("芙莉莲")
    else:
        module = _imdb_module(_imdb_title())
        meta = _meta("Breaking Bad", year="2008")

    sync_results = module.search_medias(meta, media_source=selected_source)
    async_results = asyncio.run(
        module.async_search_medias(meta, media_source=selected_source)
    )

    assert (sync_results is None) is expected_none
    assert (async_results is None) is expected_none
    if expected_none:
        return
    assert [_signature(item) for item in sync_results] == [
        _signature(item) for item in async_results
    ]


@pytest.mark.parametrize(
    ("media_source", "expected"),
    [
        (MediaSource.Douban, True),
        (MediaSource.TMDB, False),
    ],
)
def test_douban_video_recognition_sync_async_decision_parity(
    media_source: MediaSource,
    expected: bool,
) -> None:
    """豆瓣影视来源准入、原生 ID 规范化和详情投影在双 ABI 下应一致。"""
    info = {
        "id": "200",
        "title": "测试电影",
        "type": "movie",
        "year": "2024",
    }
    module = DoubanModule()
    module.douban_info = Mock(return_value=deepcopy(info))
    module.async_douban_info = AsyncMock(return_value=deepcopy(info))

    sync_result = module.recognize_media(
        mtype=MediaType.MOVIE,
        media_source=media_source,
        media_id="200",
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            mtype=MediaType.MOVIE,
            media_source=media_source,
            media_id="200",
        )
    )

    assert bool(sync_result) is expected
    assert _signature(sync_result) == _signature(async_result)
    if expected:
        module.douban_info.assert_called_once_with(
            doubanid="200", mtype=MediaType.MOVIE
        )
        module.async_douban_info.assert_awaited_once_with(
            doubanid="200", mtype=MediaType.MOVIE
        )
    else:
        module.douban_info.assert_not_called()
        module.async_douban_info.assert_not_awaited()


def test_douban_music_candidate_sync_async_decision_parity() -> None:
    """豆瓣音乐标题、艺术家和曲目候选选择仅由共享决策完成。"""
    search_payload = {
        "items": [{
            "target_type": "music",
            "target": {
                "id": "1401853",
                "title": "范特西",
                "card_subtitle": "周杰伦 / 2001",
            },
        }]
    }
    detail_payload = {
        "id": "1401853",
        "title": "范特西",
        "singer": [{"name": "周杰伦"}],
        "songs": [{
            "title": "爱在西元前",
            "track_number": 1,
            "artist_names": ["周杰伦"],
        }],
    }
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.music_search.return_value = deepcopy(search_payload)
    module.doubanapi.music_detail.return_value = deepcopy(detail_payload)
    module.doubanapi.async_music_search = AsyncMock(
        return_value=deepcopy(search_payload)
    )
    module.doubanapi.async_music_detail = AsyncMock(
        return_value=deepcopy(detail_payload)
    )
    meta = MetaMusic(
        title="爱在西元前",
        album="范特西",
        artists=["周杰伦"],
    )

    sync_result = module.recognize_media(
        meta=meta,
        mtype=MediaType.MUSIC,
        media_source=MediaSource.DoubanMusic,
        music_type=MUSIC_ENTITY_RECORDING,
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=meta,
            mtype=MediaType.MUSIC,
            media_source=MediaSource.DoubanMusic,
            music_type=MUSIC_ENTITY_RECORDING,
        )
    )

    assert _music_signature(sync_result) == _music_signature(async_result)
    assert sync_result and sync_result.media_id == "1401853:1"
    module.doubanapi.music_search.assert_called_once_with(
        keyword="范特西", count=20
    )
    module.doubanapi.async_music_search.assert_awaited_once_with(
        keyword="范特西", count=20
    )


def test_musicbrainz_candidate_sync_async_decision_parity() -> None:
    """MusicBrainz 候选选择和兜底决策一致，仅检索 I/O 不同。"""
    wrong = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="wrong",
        title="晴天",
        artists=["其他歌手"],
    )
    expected = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        album_id="album-1",
    )
    module = MusicBrainzModule()
    module.cache = None
    module._search_recordings = Mock(return_value=[wrong, expected])
    module._async_search_recordings = AsyncMock(return_value=[wrong, expected])
    module._search_albums = Mock(return_value=[])
    module._async_search_albums = AsyncMock(return_value=[])
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美")

    sync_result = module.recognize_media(meta=meta, cache=False)
    async_result = asyncio.run(module.async_recognize_media(meta=meta, cache=False))

    assert _music_signature(sync_result) == _music_signature(async_result)
    assert sync_result and sync_result.media_id == "recording-1"
    module._search_recordings.assert_called_once_with(meta, limit=10)
    module._async_search_recordings.assert_awaited_once_with(meta, limit=10)
    module._search_albums.assert_not_called()
    module._async_search_albums.assert_not_awaited()


def test_theaudiodb_album_candidate_sync_async_decision_parity() -> None:
    """TheAudioDB 专辑实体准入、候选选择和结果投影在双 ABI 下应一致。"""
    album = MusicAlbumInfo(
        media_source=MediaSource.TheAudioDB,
        media_id="2109619",
        title="Parachutes",
        artists=["Coldplay"],
    )
    module = TheAudioDbModule()
    module._search_tracks = Mock(return_value=[])
    module._async_search_tracks = AsyncMock(return_value=[])
    module._search_albums = Mock(return_value=[album])
    module._async_search_albums = AsyncMock(return_value=[album])
    meta = MetaMusic(
        title="Parachutes",
        album="Parachutes",
        artists=["Coldplay"],
    )

    sync_result = module.recognize_media(
        meta=meta,
        mtype=MediaType.MUSIC,
        media_source=MediaSource.TheAudioDB,
        music_type=MUSIC_ENTITY_ALBUM,
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=meta,
            mtype=MediaType.MUSIC,
            media_source=MediaSource.TheAudioDB,
            music_type=MUSIC_ENTITY_ALBUM,
        )
    )

    assert _music_signature(sync_result) == _music_signature(async_result)
    assert sync_result and sync_result.media_id == "2109619"
    module._search_tracks.assert_not_called()
    module._async_search_tracks.assert_not_awaited()
    module._search_albums.assert_called_once_with(meta)
    module._async_search_albums.assert_awaited_once_with(meta)
