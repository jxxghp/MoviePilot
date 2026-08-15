"""
Seerr 的 Sonarr 兼容端点回归测试。

Seerr 提交剧集请求时，POST /api/v3/series 请求体不携带 tmdbId、只携带 tvdbId，
且季列表由 lookup 接口返回的季列表构造；lookup 未返回季信息时请求体季列表为空。
本测试保证新增剧集订阅在以上两种情况下都能正常创建订阅，且不会静默成功。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.servarr import arr_add_series, arr_series_lookup
from app.schemas import SonarrSeason, SonarrSeries
from app.schemas.types import MediaSource, MediaType

_TVDB_ID = 454898
_TMDB_ID = 236534


def _fake_mediainfo(tmdb_id=_TMDB_ID, seasons=None):
    """构造最小可用的媒体识别结果。"""
    return SimpleNamespace(
        tmdb_id=tmdb_id,
        title="Tales of Herding Gods",
        year=2024,
        imdb_id=None,
        seasons=seasons or {1: [1, 2, 3]},
        get_poster_image=lambda: None,
    )


def _series(tmdb_id=None, seasons=None):
    """构造 Seerr 风格的剧集请求体，默认不携带 tmdbId。"""
    return SonarrSeries(
        id=None,
        title="Tales of Herding Gods",
        tvdbId=_TVDB_ID,
        tmdbId=tmdb_id,
        year=2024,
        seasons=seasons or [],
    )


def _run_add(tv):
    """直接调用新增剧集订阅处理函数。"""
    return asyncio.run(arr_add_series(tv=tv, _="api-token", db=object()))


def _patch_chains(mediainfo=None, exists=None, add_result=(123, "")):
    """统一 patch 媒体链、订阅链与订阅查询。"""
    media_chain = MagicMock()
    media_chain.tvdb_info.return_value = {
        "name": "Tales of Herding Gods",
        "seasons": [{"type": {"id": "default"}, "number": 1}],
        "defaultSeasonType": "default",
    }
    media_chain.recognize_by_meta.return_value = mediainfo
    subscribe_chain = MagicMock()
    subscribe_chain.async_add = AsyncMock(return_value=add_result)
    return patch(
        "app.api.servarr.MediaChain",
        return_value=media_chain,
    ), patch(
        "app.api.servarr.SubscribeChain",
        return_value=subscribe_chain,
    ), patch(
        "app.api.servarr.Subscribe.async_exists",
        new=AsyncMock(return_value=exists),
    ), subscribe_chain


def test_add_series_without_tmdbid_resolves_identity_via_tvdbid():
    """Seerr 请求体不携带 tmdbId 时，应按 tvdbId 补全媒体身份并创建订阅。"""
    tv = _series(seasons=[SonarrSeason(seasonNumber=1, monitored=True)])
    media_patch, chain_patch, exists_patch, subscribe_chain = _patch_chains(
        mediainfo=_fake_mediainfo()
    )
    with media_patch, chain_patch, exists_patch:
        result = _run_add(tv)

    assert result.id == 123
    subscribe_chain.async_add.assert_awaited_once_with(
        title="Tales of Herding Gods",
        year=2024,
        season=1,
        media_source=MediaSource.TMDB,
        media_id=str(_TMDB_ID),
        mtype=MediaType.TV,
        username="Seerr",
    )


def test_add_series_with_empty_seasons_falls_back_to_all_seasons():
    """请求体季列表为空时不应静默成功，应兜底订阅已识别的全部季。"""
    tv = _series()
    media_patch, chain_patch, exists_patch, subscribe_chain = _patch_chains(
        mediainfo=_fake_mediainfo(seasons={1: [1, 2, 3], 2: [1]})
    )
    subscribe_chain.async_add = AsyncMock(side_effect=[(100, ""), (101, "")])
    with media_patch, chain_patch, exists_patch:
        result = _run_add(tv)

    assert result.id == 101
    assert subscribe_chain.async_add.await_count == 2
    assert subscribe_chain.async_add.await_args_list[0].kwargs["season"] == 1
    assert subscribe_chain.async_add.await_args_list[1].kwargs["season"] == 2


def test_add_series_already_subscribed_returns_existing():
    """全部请求季已存在订阅时，返回已有标识且不重复创建。"""
    tv = _series(
        tmdb_id=_TMDB_ID,
        seasons=[SonarrSeason(seasonNumber=1, monitored=True)],
    )
    media_patch, chain_patch, exists_patch, subscribe_chain = _patch_chains(
        exists=SimpleNamespace(id=9)
    )
    with media_patch, chain_patch, exists_patch:
        result = _run_add(tv)

    assert result.id == 1
    subscribe_chain.async_add.assert_not_awaited()


def test_add_series_identity_resolution_failure_returns_500():
    """媒体身份补全失败时返回 500，避免 Seerr 误判请求已成功。"""
    tv = _series()
    media_patch, chain_patch, exists_patch, _ = _patch_chains(mediainfo=None)
    with media_patch, chain_patch, exists_patch:
        with pytest.raises(HTTPException) as excinfo:
            _run_add(tv)

    assert excinfo.value.status_code == 500


def test_series_lookup_falls_back_to_tmdb_seasons():
    """TVDB 未提供可用季信息时，lookup 应按 TMDB 季集兜底返回季列表。"""
    media_chain = MagicMock()
    media_chain.tvdb_info.return_value = {
        "name": "Tales of Herding Gods",
        "seasons": [],
        "defaultSeasonType": "default",
    }
    media_chain.recognize_by_meta.return_value = _fake_mediainfo(
        seasons={1: [1, 2, 3], 2: [1]}
    )
    media_chain.media_exists.return_value = False
    with patch(
        "app.api.servarr.TvdbChain",
        return_value=MagicMock(get_tvdbid_by_name=MagicMock(return_value=[_TVDB_ID])),
    ), patch(
        "app.api.servarr.MediaChain",
        return_value=media_chain,
    ), patch(
        "app.api.servarr.Subscribe.list_by_media_identity",
        return_value=[],
    ):
        result = arr_series_lookup(term=f"tvdb:{_TVDB_ID}", _="api-token", db=object())

    assert len(result) == 1
    assert [season.seasonNumber for season in result[0].seasons] == [1, 2]
    assert all(not season.monitored for season in result[0].seasons)