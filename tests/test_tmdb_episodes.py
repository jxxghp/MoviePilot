"""
验证 TMDB 季集列表保留单集标识。

剧集组会把集号改成组内序号，客户端需要单集标识才能稳定对应媒体服务器中的集。
"""

import asyncio
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import tmdb as tmdb_endpoint
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.schemas.tmdb import TmdbEpisode

GROUP_ID = "5ad0ec240e0a26303f00d84d"


def _module_with_resources(
    season_detail: Optional[dict] = None, group_episodes: Optional[list] = None
) -> TheMovieDbModule:
    """
    构造只替换 TMDB 底层资源对象的模块，保留季详情与剧集组重排的真实实现。

    :param season_detail: 默认顺序季详情接口的原始返回
    :param group_episodes: 剧集组接口的原始分组列表
    :return: 不触发模块生命周期的 TMDB 模块
    """
    api = object.__new__(TmdbApi)
    api.season_obj = Mock()
    api.season_obj.details.return_value = season_detail
    api.season_obj.async_details = AsyncMock(return_value=season_detail)
    api.tv = Mock()
    api.tv.group_episodes.return_value = group_episodes
    api.tv.async_group_episodes = AsyncMock(return_value=group_episodes)
    module = object.__new__(TheMovieDbModule)
    module.tmdb = api
    return module


def _both_entries(
    module: TheMovieDbModule, season: int, episode_group: Optional[str] = None
) -> list[list[TmdbEpisode]]:
    """
    分别经同步与异步入口读取同一季的集列表。

    :param module: 待测 TMDB 模块
    :param season: 请求季号；带剧集组时为组内季号
    :param episode_group: 可选剧集组标识
    :return: 同步结果与异步结果
    """
    return [
        module.tmdb_episodes(tmdbid=100, season=season, episode_group=episode_group),
        asyncio.run(
            module.async_tmdb_episodes(tmdbid=100, season=season, episode_group=episode_group)
        ),
    ]


def test_default_season_keeps_episode_id() -> None:
    """
    默认顺序的季集列表应保留 TMDB 返回的单集标识。
    """
    module = _module_with_resources(
        season_detail={
            "episodes": [
                {"id": 2001, "season_number": 2, "episode_number": 1, "name": "第一集"},
                {"id": 2003, "season_number": 2, "episode_number": 3, "name": "第三集"},
            ]
        }
    )

    for episodes in _both_entries(module, season=2):
        assert [(item.id, item.season_number, item.episode_number) for item in episodes] == [
            (2001, 2, 1),
            (2003, 2, 3),
        ]


def test_episode_group_keeps_episode_id_after_renumbering() -> None:
    """
    剧集组把集号改为组内序号后，单集标识仍应指向原单集，原季号保持不变。
    """
    module = _module_with_resources(
        group_episodes=[
            {
                "order": 1,
                "episodes": [
                    {"id": 2003, "season_number": 2, "episode_number": 3},
                    {"id": 1010, "season_number": 1, "episode_number": 10},
                ],
            },
            {"order": 2, "episodes": [{"id": 3001, "season_number": 3, "episode_number": 1}]},
        ]
    )

    for episodes in _both_entries(module, season=1, episode_group=GROUP_ID):
        assert [(item.id, item.season_number, item.episode_number) for item in episodes] == [
            (2003, 2, 1),
            (1010, 1, 2),
        ]


def test_episode_without_id_stays_in_list() -> None:
    """
    原始数据缺少单集标识时该字段为空，同批其他集照常返回。
    """
    module = _module_with_resources(
        season_detail={
            "episodes": [
                {"season_number": 1, "episode_number": 1, "name": "缺少标识"},
                {"id": 1002, "season_number": 1, "episode_number": 2, "name": "第二集"},
            ]
        }
    )

    for episodes in _both_entries(module, season=1):
        assert [(item.id, item.name) for item in episodes] == [
            (None, "缺少标识"),
            (1002, "第二集"),
        ]


def test_season_episodes_response_includes_episode_id(monkeypatch) -> None:
    """
    季集接口响应应输出单集标识，缺失时显式输出 null。
    """
    episodes = AsyncMock(
        return_value=[
            TmdbEpisode(id=1001, season_number=1, episode_number=1),
            TmdbEpisode(season_number=1, episode_number=2),
        ]
    )
    monkeypatch.setattr(
        tmdb_endpoint, "TmdbChain", lambda: SimpleNamespace(async_tmdb_episodes=episodes)
    )
    app = FastAPI()
    app.dependency_overrides[tmdb_endpoint.verify_token] = lambda: None
    app.include_router(tmdb_endpoint.router, prefix="/api/v1/tmdb")

    with TestClient(app) as client:
        response = client.get("/api/v1/tmdb/100/1", params={"episode_group": GROUP_ID})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [1001, None]
    episodes.assert_awaited_once_with(tmdbid=100, season=1, episode_group=GROUP_ID)
