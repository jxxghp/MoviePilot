"""AniList、Bangumi 与 IMDb Module 同步/异步决策一致性测试。"""

import asyncio
from copy import deepcopy
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.anilist import AniListModule
from app.modules.bangumi import BangumiModule
from app.modules.imdb import ImdbModule
from app.modules.imdb.api import ImdbAka, ImdbApi, ImdbTitle
from app.schemas.types import MediaSource, MediaType


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
