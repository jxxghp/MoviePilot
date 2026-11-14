"""
验证 AniList 浏览筛选、官方查询的中文合并及季节参数映射。
"""

import asyncio
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.endpoints import anilist as anilist_endpoint
from app.domain.context import MediaInfo
from app.modules.anilist import AniListModule
from app.modules.anilist import anilist as anilist_client
from app.modules.anilist.anilist import AniListApi


@pytest.fixture(autouse=True)
def isolated_translations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    每个用例使用独立缓存目录及原始下载地址，防止中文数据跨用例串扰。
    """
    runtime_setting = anilist_client.get_runtime_setting
    values = {"CACHE_PATH": tmp_path, "GITHUB_PROXY": None}
    monkeypatch.setattr(anilist_client, "get_runtime_setting", lambda name: values[name] if name in values else runtime_setting(name))


def _media_info(anilist_id: int = 154587) -> dict:
    """
    构造 AniList 榜单测试媒体。

    :param anilist_id: AniList 媒体 ID
    :return: AniList 媒体信息
    """
    return {
        "id": anilist_id,
        "title": {
            "romaji": "Sousou no Frieren",
            "english": "Frieren: Beyond Journey's End",
            "native": "葬送のフリーレン",
            "chinese": "葬送的芙莉莲",
        },
        "synonyms": ["葬送的芙莉莲"],
        "format": "TV",
        "startDate": {"year": 2023, "month": 9, "day": 29},
        "coverImage": {"large": "https://img.example/poster.jpg"},
        "genres": ["Fantasy"],
        "averageScore": 91,
        "isAdult": False,
    }


def test_anilist_client_forwards_discover_filters() -> None:
    """
    AniList 探索应原样传递组合过滤条件。
    """
    client = AniListApi()
    client._invoke = Mock(return_value={"Page": {"media": [_media_info()]}})

    medias = client.discover(
        page=2,
        count=24,
        search="Frieren",
        genre="Fantasy",
        media_format="TV",
        season="FALL",
        season_year=2023,
        status="FINISHED",
        country="JP",
        sort="SCORE_DESC",
    )

    assert medias[0]["id"] == 154587
    variables = client._invoke.call_args.args[1]
    assert variables == {
        "page": 2,
        "count": 24,
        "search": "Frieren",
        "genre": "Fantasy",
        "format": "TV",
        "season": "FALL",
        "seasonYear": 2023,
        "status": "FINISHED",
        "country": "JP",
        "sort": ["SCORE_DESC"],
    }
    assert "id" in client._page_query
    assert "synonyms" in client._page_query


def test_anilist_client_queries_official_and_merges_chinese_dataset() -> None:
    """
    直接请求官方接口并合并项目中文数据，避免探测已失效的中文代理。
    """
    official_media = _media_info()
    official_media["title"].pop("chinese")
    official_media["synonyms"] = ["Official Alias"]
    official_response = Mock(status_code=200)
    official_response.json.return_value = {
        "data": {"Page": {"media": [official_media]}}
    }
    client = AniListApi()
    client._request = Mock()
    client._request.post_res.return_value = official_response
    client._request.get_json.return_value = [
        {
            "id": 154587,
            "title": "葬送的芙莉莲",
            "synonyms": ["Frieren at the Funeral"],
        }
    ]

    result = client._invoke("query", {"page": 1})

    assert result["Page"]["media"][0]["title"]["chinese"] == "葬送的芙莉莲"
    assert result["Page"]["media"][0]["synonyms"] == [
        "Official Alias",
        "Frieren at the Funeral",
    ]
    assert [call.args[0] for call in client._request.post_res.call_args_list] == [
        client._official_url,
    ]
    client._request.get_json.assert_called_once_with(client._translations_url)


def test_anilist_async_client_queries_official_without_real_network() -> None:
    """
    异步查询也应直接使用官方接口和同一中文数据集。
    """
    official_media = _media_info()
    official_media["title"].pop("chinese")
    official_response = Mock(status_code=200)
    official_response.json.return_value = {
        "data": {"Page": {"media": [official_media]}}
    }
    client = AniListApi()
    client._async_request = Mock()
    client._async_request.post_res = AsyncMock(
        return_value=official_response
    )
    client._async_request.get_json = AsyncMock(
        return_value=[
            {"id": 154587, "title": "葬送的芙莉莲", "synonyms": []}
        ]
    )

    result = asyncio.run(client._async_invoke("query", {"page": 1}))

    assert result["Page"]["media"][0]["title"]["chinese"] == "葬送的芙莉莲"
    assert [call.args[0] for call in client._async_request.post_res.call_args_list] == [
        client._official_url,
    ]
    client._async_request.get_json.assert_awaited_once_with(client._translations_url)


def test_anilist_current_season_maps_calendar_quarters() -> None:
    """
    AniList 本季榜应按自然季度映射四季枚举。
    """
    assert AniListApi._current_season(date(2026, 1, 15)) == ("WINTER", 2026)
    assert AniListApi._current_season(date(2026, 4, 15)) == ("SPRING", 2026)
    assert AniListApi._current_season(date(2026, 7, 15)) == ("SUMMER", 2026)
    assert AniListApi._current_season(date(2026, 10, 15)) == ("FALL", 2026)


def test_anilist_nested_media_relations_are_requeried_through_page() -> None:
    """
    相关推荐和人物作品应通过根级 Page.media 回查以触发中文标题注入。
    """
    recommendation_client = AniListApi()
    recommendation_client._invoke = Mock(
        side_effect=[
            {
                "Media": {
                    "recommendations": {
                        "nodes": [
                            {"mediaRecommendation": {"id": 20}},
                            {"mediaRecommendation": {"id": 10}},
                        ]
                    }
                }
            },
            {"Page": {"media": [_media_info(10), _media_info(20)]}},
        ]
    )

    recommendations = recommendation_client.recommendations(154587)

    assert [media["id"] for media in recommendations] == [20, 10]
    assert recommendation_client._invoke.call_args_list[1].args[1] == {
        "ids": [20, 10],
        "count": 2,
    }
    assert "Page(page: 1" in recommendation_client._invoke.call_args_list[1].args[0]

    credits_client = AniListApi()
    credits_client._invoke = Mock(
        side_effect=[
            {"Staff": {"characterMedia": {"nodes": [{"id": 30}]}}},
            {"Page": {"media": [_media_info(30)]}},
        ]
    )

    credits = credits_client.person_credits(95075)

    assert [media["id"] for media in credits] == [30]
    person_query = credits_client._invoke.call_args_list[0].args[0]
    assert "characterMedia" in person_query
    assert "staffMedia" not in person_query
    assert "Page(page: 1" in credits_client._invoke.call_args_list[1].args[0]


def test_anilist_async_person_credits_uses_character_media() -> None:
    """
    异步人物作品查询应读取配音角色关联，而不是制作岗位关联。
    """
    client = AniListApi()
    client._async_invoke = AsyncMock(
        side_effect=[
            {"Staff": {"characterMedia": {"nodes": [{"id": 31}]}}},
            {"Page": {"media": [_media_info(31)]}},
        ]
    )

    credits = asyncio.run(client.async_person_credits(95076))

    assert [media["id"] for media in credits] == [31]
    person_query = client._async_invoke.call_args_list[0].args[0]
    assert "characterMedia" in person_query
    assert "staffMedia" not in person_query
    assert "Page(page: 1" in client._async_invoke.call_args_list[1].args[0]


def test_anilist_credits_and_recommendations_use_separate_caches() -> None:
    """
    相同分页参数的演员与推荐查询不得互相命中缓存。
    """
    client = AniListApi()
    client._invoke = Mock(
        side_effect=[
            {"Media": {"characters": {"edges": [{"role": "MAIN"}]}}},
            {
                "Media": {
                    "recommendations": {
                        "nodes": [{"mediaRecommendation": {"id": 40}}]
                    }
                }
            },
            {"Page": {"media": [_media_info(40)]}},
        ]
    )

    credits = client.credits(987654, page=1, count=20)
    recommendations = client.recommendations(987654, page=1, count=20)

    assert credits == [{"role": "MAIN"}]
    assert [media["id"] for media in recommendations] == [40]
    assert client._invoke.call_count == 3


def test_anilist_module_normalizes_voice_actor_and_person_detail() -> None:
    """
    AniList 模块应把配音关系和人物详情转换为前端通用人物结构。
    """
    module = AniListModule()
    module.anilist_api = Mock()
    module.anilist_api.credits.return_value = [
        {
            "node": {"name": {"full": "Frieren", "native": "フリーレン"}},
            "voiceActors": [
                {
                    "id": 95075,
                    "name": {
                        "full": "Atsumi Tanezaki",
                        "native": "種﨑敦美",
                        "alternative": [],
                    },
                    "image": {"large": "https://img.example/actor.jpg"},
                    "siteUrl": "https://anilist.co/staff/95075",
                }
            ],
        }
    ]
    module.anilist_api.person_detail.return_value = {
        "id": 95075,
        "name": {
            "full": "Atsumi Tanezaki",
            "native": "種﨑敦美",
            "alternative": ["Atsumi Tanezaki"],
        },
        "image": {"large": "https://img.example/actor.jpg"},
        "description": "日本声优",
        "dateOfBirth": {"year": 1990, "month": 9, "day": 27},
        "homeTown": "Oita",
        "primaryOccupations": ["Voice Actor"],
    }

    credits = module.anilist_credits(154587)
    person = module.anilist_person_detail(95075)

    assert credits[0].source == "anilist"
    assert credits[0].name == "種﨑敦美"
    assert credits[0].character == "フリーレン"
    assert credits[0].images is not None
    assert credits[0].images.large == "https://img.example/actor.jpg"
    assert person is not None
    assert person.birthday == "1990-09-27"
    assert person.career == ["Voice Actor"]


def test_anilist_discover_endpoint_forwards_all_filters(monkeypatch) -> None:
    """
    AniList 独立探索端点应把全部过滤条件交给处理链。
    """
    captured = {}
    chain = Mock()
    chain.async_discover = AsyncMock(
        return_value=[MediaInfo(anilist_info=_media_info())]
    )
    monkeypatch.setattr(anilist_endpoint, "AniListChain", lambda: chain)

    result = asyncio.run(
        anilist_endpoint.anilist_discover(
            page=3,
            count=16,
            search="Frieren",
            genre="Fantasy",
            media_format="TV",
            season="FALL",
            season_year=2023,
            status="FINISHED",
            country="JP",
            sort="TRENDING_DESC",
            _=None,
        )
    )
    captured.update(chain.async_discover.await_args.kwargs)

    assert captured == {
        "page": 3,
        "count": 16,
        "search": "Frieren",
        "genre": "Fantasy",
        "media_format": "TV",
        "season": "FALL",
        "season_year": 2023,
        "status": "FINISHED",
        "country": "JP",
        "sort": "TRENDING_DESC",
    }
    assert result[0].anilist_id == 154587


def test_anilist_router_exposes_browse_and_deep_navigation_paths() -> None:
    """
    AniList 独立路由应覆盖榜单、探索、作品、演员和相关推荐。
    """
    paths = {route.path for route in anilist_endpoint.router.routes}

    assert paths == {
        "/trending",
        "/popular-this-season",
        "/discover",
        "/credits/{anilist_id}",
        "/recommend/{anilist_id}",
        "/person/{person_id}",
        "/person/credits/{person_id}",
        "/{anilist_id}",
    }
