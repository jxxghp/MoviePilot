"""
验证剧集组缺集检查使用所选分季结构，并保留默认季与已入库季的接口语义。
"""

from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import mediaserver
from app.chain.download.existence import DownloadExistenceOwner
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import ExistMediaInfo, NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType

# Issue #7252 中《一念永恒》的默认合并季与所选分季剧集组。
_EPISODE_GROUP = "6580f4c53a48c556a0af6c00"
_GROUP_TOTALS = {1: 52, 2: 54, 3: 59, 4: 35}


def _media(seasons: dict[int, list[int]], episode_group: Optional[str] = _EPISODE_GROUP) -> MediaInfo:
    """
    创建同一媒体在默认季或剧集组口径下的识别信息。

    :param seasons: 本次识别采用的各季集号
    :param episode_group: 所选剧集组，None 表示默认季结构
    :return: 具有稳定 TMDB 身份的媒体信息
    """
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="107371",
        tmdb_id=107371,
        type=MediaType.TV,
        title="一念永恒",
        year="2020",
        seasons=seasons,
        episode_group=episode_group,
    )


@pytest.fixture
def grouped_existence(monkeypatch: pytest.MonkeyPatch) -> tuple[DownloadExistenceOwner, Mock, Mock]:
    """
    保留真实缺集算法，仅隔离媒体识别和媒体服务器查询。

    :param monkeypatch: 用例结束后恢复识别和服务器查询边界的夹具
    :return: 缺集计算对象、识别调用和媒体服务器查询替身
    """
    owner = DownloadExistenceOwner.__new__(DownloadExistenceOwner)
    recognize = Mock(return_value=_media({
        season: list(range(1, total + 1)) for season, total in _GROUP_TOTALS.items()
    }))
    exists = Mock(return_value=None)
    monkeypatch.setattr("app.chain.download.existence.MediaChain", lambda: SimpleNamespace(
        recognize_media=recognize,
    ))
    monkeypatch.setattr(owner, "_media_exists_with_server_cache", exists)
    return owner, recognize, exists


@pytest.mark.parametrize("cached_seasons", [False, True])
def test_notexists_api_returns_all_group_seasons(grouped_existence, monkeypatch, cached_seasons) -> None:
    """
    请求携带默认合并季时，真实 HTTP 入口仍返回剧集组四季的缺失信息。
    """
    owner, recognize, exists = grouped_existence
    monkeypatch.setattr(mediaserver, "DownloadChain", lambda: owner)
    app = FastAPI()
    app.dependency_overrides[mediaserver.verify_token] = lambda: None
    app.include_router(mediaserver.router, prefix="/api/v1/mediaserver")
    payload = {
        "media_source": "themoviedb", "media_id": "107371", "tmdb_id": 107371,
        "title": "一念永恒", "year": "2020", "type": "电视剧",
        "episode_group": _EPISODE_GROUP,
        "seasons": {"1": list(range(1, 201))} if cached_seasons else {},
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/mediaserver/notexists", json=payload)

    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True
    assert {item["season"]: item["total_episode"] for item in result["data"]} == _GROUP_TOTALS
    assert all(item["episodes"] == [] and item["start_episode"] == 1 for item in result["data"])
    recognize.assert_called_once_with(
        mtype=MediaType.TV, media_source=MediaSource.TMDB, media_id="107371", episode_group=_EPISODE_GROUP,
    )
    assert exists.call_args.kwargs["mediainfo"] is recognize.return_value


@pytest.mark.parametrize("complete", [False, True])
def test_group_existence_compares_library_in_selected_seasons(grouped_existence, complete) -> None:
    """
    按组分季计算部分缺集，并继续从缺失结果中省略已完整入库的季。
    """
    owner, recognize, exists = grouped_existence
    library_seasons = {season: episodes.copy() for season, episodes in recognize.return_value.seasons.items()}
    if not complete:
        library_seasons[2] = [1, 2]
        del library_seasons[3]
    exists.return_value = ExistMediaInfo(type=MediaType.TV, seasons=library_seasons)

    exist_flag, missing = owner.get_no_exists_info(MetaInfo("一念永恒"), _media({1: list(range(1, 201))}))

    assert exist_flag is complete
    recognize.assert_called_once()
    if complete:
        assert missing == {}
    else:
        assert set(missing["tmdb:107371"]) == {2, 3}
        assert sorted(missing["tmdb:107371"][2].episodes) == list(range(3, 55))
        assert missing["tmdb:107371"][2].total_episode == 54
        assert missing["tmdb:107371"][3].episodes == []
        assert missing["tmdb:107371"][3].total_episode == 59


@pytest.mark.parametrize("season", [0, 2, 4])
def test_group_existence_preserves_selected_season_and_total(grouped_existence, season) -> None:
    """
    剧集组重识别后仍按入口指定季和总集数检查，包括第零季。
    """
    owner, recognize, _ = grouped_existence
    recognize.return_value.seasons[0] = [1, 2]
    meta = MetaInfo("一念永恒")
    meta.begin_season = season

    exist_flag, missing = owner.get_no_exists_info(
        meta, _media({1: list(range(1, 201))}), totals={season: 60},
    )

    assert exist_flag is False
    assert set(missing["tmdb:107371"]) == {season}
    assert missing["tmdb:107371"][season].total_episode == 60


@pytest.mark.parametrize("cached_seasons", [False, True])
def test_default_seasons_only_recognize_when_missing(grouped_existence, cached_seasons) -> None:
    """
    未选择剧集组时复用已有季信息，季信息为空才补充识别。
    """
    owner, recognize, _ = grouped_existence
    seasons = {1: list(range(1, 201))}
    recognize.return_value = _media(seasons, episode_group=None)

    exist_flag, missing = owner.get_no_exists_info(
        MetaInfo("一念永恒"), _media(seasons if cached_seasons else {}, episode_group=None),
    )

    assert exist_flag is False
    assert set(missing["tmdb:107371"]) == {1}
    assert missing["tmdb:107371"][1].total_episode == 200
    assert recognize.call_count == int(not cached_seasons)


@pytest.mark.parametrize("recognized", [None, _media({})])
def test_failed_group_recognition_does_not_reuse_default_seasons(grouped_existence, recognized) -> None:
    """
    剧集组识别失败或没有季信息时，保留原失败语义且不查询默认合并季。
    """
    owner, recognize, exists = grouped_existence
    recognize.return_value = recognized

    assert owner.get_no_exists_info(MetaInfo("一念永恒"), _media({1: list(range(1, 201))})) == (False, {})
    recognize.assert_called_once()
    exists.assert_not_called()


def test_group_existence_preserves_accumulated_missing_media(grouped_existence) -> None:
    """
    重新识别剧集组只追加当前媒体缺集，不丢弃调用方已收集的其他媒体。
    """
    owner, _, _ = grouped_existence
    previous = {"douban:123": {1: NotExistMediaInfo(season=1, total_episode=12)}}

    exist_flag, missing = owner.get_no_exists_info(
        MetaInfo("一念永恒"), _media({1: list(range(1, 201))}), no_exists=previous,
    )

    assert exist_flag is False
    assert missing is previous
    assert missing["douban:123"][1].total_episode == 12
    assert set(missing["tmdb:107371"]) == set(_GROUP_TOTALS)
