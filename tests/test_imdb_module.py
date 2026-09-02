"""IMDb 原生媒体数据源 Module 测试。"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.imdb import ImdbModule
from app.modules.imdb.api import (
    ImdbAka,
    ImdbApi,
    ImdbCredit,
    ImdbDate,
    ImdbEpisode,
    ImdbImage,
    ImdbPerson,
    ImdbSeason,
    ImdbTitle,
)
from app.runtime.config import settings
from app.runtime.tasks import TaskRegistry
from app.schemas.types import MediaRecognizeType, MediaSource, MediaType


@pytest.fixture
def imdb_title() -> ImdbTitle:
    """构造一条包含完整基础字段的 IMDb 电视剧条目。"""
    return ImdbTitle(
        id="tt0903747",
        type="tvSeries",
        primaryTitle="Breaking Bad",
        originalTitle="Breaking Bad",
        primaryImage={"url": "https://image.example/poster.jpg", "type": "poster"},
        startYear=2008,
        runtimeSeconds=2820,
        genres=["Crime", "Drama"],
        rating={"aggregateRating": 9.5, "voteCount": 2200000},
        plot="A chemistry teacher becomes a methamphetamine producer.",
        originCountries=[{"code": "US", "name": "United States"}],
        spokenLanguages=[{"code": "eng", "name": "English"}],
    )


@pytest.fixture
def imdb_api(imdb_title: ImdbTitle) -> Mock:
    """构造同步和异步方法行为一致的 IMDb API 替身。"""
    api = Mock(spec=ImdbApi)
    api.search_titles.return_value = [imdb_title]
    api.get_title.return_value = imdb_title
    api.list_akas.return_value = [ImdbAka(text="绝命毒师")]
    api.list_credits.return_value = [
        ImdbCredit(
            name=ImdbPerson(
                id="nm0533713",
                displayName="Vince Gilligan",
                primaryImage=ImdbImage(url="https://image.example/director.jpg"),
            ),
            category="DIRECTOR",
        ),
        ImdbCredit(
            name=ImdbPerson(
                id="nm0186505",
                displayName="Bryan Cranston",
                primaryImage=ImdbImage(url="https://image.example/actor.jpg"),
            ),
            category="ACTOR",
            characters=["Walter White"],
        ),
    ]
    api.list_images.return_value = [
        ImdbImage(url="https://image.example/backdrop.jpg", type="still_frame")
    ]
    api.list_episodes.return_value = [
        ImdbEpisode(
            id="tt0959621",
            season="1",
            episodeNumber=1,
            releaseDate=ImdbDate(year=2008, month=1, day=20),
        )
    ]
    api.list_seasons.return_value = [ImdbSeason(season="1", episodeCount=7)]
    api.async_search_titles = AsyncMock(return_value=[imdb_title])
    api.async_get_title = AsyncMock(return_value=imdb_title)
    api.async_list_akas = AsyncMock(return_value=api.list_akas.return_value)
    api.async_list_credits = AsyncMock(return_value=api.list_credits.return_value)
    api.async_list_images = AsyncMock(return_value=api.list_images.return_value)
    api.async_list_episodes = AsyncMock(return_value=api.list_episodes.return_value)
    api.async_list_seasons = AsyncMock(return_value=api.list_seasons.return_value)
    return api


@pytest.fixture
def imdb_module(imdb_api: Mock) -> ImdbModule:
    """构造注入离线 API 替身的 IMDb Module。"""
    module = ImdbModule()
    module.imdb_api = imdb_api
    module.scraper = Mock()
    return module


def test_imdb_module_declares_independent_media_recognizer() -> None:
    """IMDb 应作为独立宿主媒体识别 Module 暴露稳定元数据。"""
    assert ImdbModule.get_name() == "IMDb"
    assert ImdbModule.get_subtype() == MediaRecognizeType.IMDb
    assert ImdbModule.get_priority() == 4


def test_imdb_api_search_uses_keyless_imdb_suggestion_endpoint() -> None:
    """IMDb 标题搜索应直接调用 IMDb 免 Key 建议接口并缓存响应。"""
    api = ImdbApi()
    api.clear_cache()
    api._request = Mock()
    api._request.get_json.return_value = {
        "d": [
            {
                "id": "tt0111161",
                "qid": "movie",
                "l": "The Shawshank Redemption",
                "y": 1994,
            }
        ]
    }

    results = api.search_titles("The Shawshank Redemption", limit=1)
    cached_results = api.search_titles("The Shawshank Redemption", limit=1)

    assert [item.id for item in results] == ["tt0111161"]
    assert [item.id for item in cached_results] == ["tt0111161"]
    api._request.get_json.assert_called_once_with(
        "https://v2.sg.media-imdb.com/suggestion/x/"
        "The%20Shawshank%20Redemption.json",
        params={},
    )

    api.clear_cache()
    api.search_titles("The Shawshank Redemption", limit=1)
    assert api._request.get_json.call_count == 2


def test_imdb_graphql_error_is_not_cached() -> None:
    """GraphQL 错误响应不应污染缓存，后续成功详情仍应正常写入缓存。"""
    api = ImdbApi()
    api.clear_cache()
    api._request = Mock()
    api._request.post_json.side_effect = [
        {"errors": [{"message": "temporary failure"}]},
        {
            "data": {
                "titles": [
                    {
                        "id": "tt0111161",
                        "titleText": {"text": "The Shawshank Redemption"},
                        "titleType": {"id": "movie"},
                        "releaseYear": {"year": 1994},
                    }
                ]
            }
        },
    ]

    assert api.get_title("tt0111161") is None
    detail = api.get_title("tt0111161")
    cached_detail = api.get_title("tt0111161")

    assert detail and detail.primary_title == "The Shawshank Redemption"
    assert cached_detail and cached_detail.id == "tt0111161"
    assert api._request.post_json.call_count == 2


def test_imdb_title_parser_accepts_null_optional_objects() -> None:
    """GraphQL 可选对象为 null 时应保留可用详情并归一为空值。"""
    detail = ImdbApi._parse_title(
        {
            "titles": [
                {
                    "id": "tt30836097",
                    "titleType": {"id": "tvSeries"},
                    "titleText": {"text": "19th Floor"},
                    "originalTitleText": None,
                    "releaseYear": None,
                    "runtime": None,
                    "titleGenres": None,
                    "countriesOfOrigin": None,
                    "spokenLanguages": None,
                    "plot": None,
                }
            ]
        }
    )

    assert detail is not None
    assert detail.id == "tt30836097"
    assert detail.primary_title == "19th Floor"
    assert detail.original_title is None
    assert detail.start_year is None
    assert detail.runtime_seconds is None
    assert detail.genres == []
    assert detail.origin_countries == []
    assert detail.spoken_languages == []
    assert detail.plot is None


def test_imdb_clear_cache_registers_async_cleanup_in_running_loop() -> None:
    """同步清缓存入口在异步宿主中应登记任务，并保留原同步调用约定。"""

    async def scenario() -> None:
        """验证异步缓存清理直到完成前都由宿主登记器持有。"""
        api = ImdbApi()
        registry = TaskRegistry()
        release = asyncio.Event()

        async def clear_async_cache() -> None:
            """等待测试释放，确保可以观察登记中的任务。"""
            await release.wait()

        with (
            patch("app.modules.imdb.api.get_task_registry", return_value=registry),
            patch.object(api, "async_clear_cache", side_effect=clear_async_cache),
        ):
            assert api.clear_cache() is None
            assert [record.owner for record in registry.records] == [
                "module.imdb.cache_clear"
            ]

            release.set()
            await registry.records[0].task
            await asyncio.sleep(0)

        assert registry.records == ()
        api.close()

    asyncio.run(scenario())


def test_async_imdb_api_merges_paginated_episodes() -> None:
    """IMDb 异步剧集查询应使用 GraphQL 游标合并并缓存所有分页。"""
    api = ImdbApi()
    asyncio.run(api.async_clear_cache())
    api._async_request = Mock()
    api._async_request.post_json = AsyncMock(
        side_effect=[
            {
                "data": {
                    "titles": [
                        {
                            "episodes": {
                                "episodes": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "tt-first",
                                                "series": {
                                                    "episodeNumber": {
                                                        "seasonNumber": 1,
                                                        "episodeNumber": 1,
                                                    }
                                                },
                                            }
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": "next-page",
                                    },
                                }
                            }
                        }
                    ]
                }
            },
            {
                "data": {
                    "titles": [
                        {
                            "episodes": {
                                "episodes": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "tt-second",
                                                "series": {
                                                    "episodeNumber": {
                                                        "seasonNumber": 1,
                                                        "episodeNumber": 2,
                                                    }
                                                },
                                            }
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    ]
                }
            },
            {
                "data": {
                    "titles": [
                        {
                            "episodes": {
                                "episodes": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "tt-first",
                                                "series": {
                                                    "episodeNumber": {
                                                        "seasonNumber": 1,
                                                        "episodeNumber": 1,
                                                    }
                                                },
                                            }
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": "next-page",
                                    },
                                }
                            }
                        }
                    ]
                }
            },
            {
                "data": {
                    "titles": [
                        {
                            "episodes": {
                                "episodes": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "tt-second",
                                                "series": {
                                                    "episodeNumber": {
                                                        "seasonNumber": 1,
                                                        "episodeNumber": 2,
                                                    }
                                                },
                                            }
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    ]
                }
            },
        ]
    )

    results = asyncio.run(api.async_list_episodes("tt-series"))
    cached_results = asyncio.run(api.async_list_episodes("tt-series"))

    assert [item.id for item in results] == ["tt-first", "tt-second"]
    assert [item.id for item in cached_results] == ["tt-first", "tt-second"]
    assert api._async_request.post_json.await_count == 2
    assert api._async_request.post_json.await_args_list[0].kwargs["json"][
        "variables"
    ] == {"after": None, "first": 100, "titles": ("tt-series",)}
    assert api._async_request.post_json.await_args_list[1].kwargs["json"][
        "variables"
    ] == {"after": "next-page", "first": 100, "titles": ("tt-series",)}

    asyncio.run(api.async_clear_cache())
    refreshed_results = asyncio.run(api.async_list_episodes("tt-series"))
    assert [item.id for item in refreshed_results] == ["tt-first", "tt-second"]
    assert api._async_request.post_json.await_count == 4


def test_imdb_proxy_snapshot_reloads_client_and_closes_old_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """代理变更应关闭旧 IMDb 客户端并按新代理快照重建。"""
    module = ImdbModule()
    old_client = Mock()
    new_client = Mock()
    monkeypatch.setattr(settings, "PROXY_HOST", "http://old-proxy:7890")

    with patch("app.modules.imdb.ImdbApi", side_effect=[old_client, new_client]) as api_type:
        module.init_module()
        old_proxy = settings.PROXY
        monkeypatch.setattr(settings, "PROXY_HOST", "http://new-proxy:7890")
        new_proxy = settings.PROXY
        module.on_config_changed()

    assert api_type.call_args_list[0].kwargs["proxies"] == old_proxy
    assert api_type.call_args_list[1].kwargs["proxies"] == new_proxy
    old_client.close.assert_called_once_with()
    assert module.imdb_api is new_client


def test_imdb_module_clear_cache_delegates_to_api(
    imdb_module: ImdbModule,
) -> None:
    """系统统一清缓存动作应清理 IMDb HTTP 缓存区。"""
    imdb_module.clear_cache()

    imdb_module.imdb_api.clear_cache.assert_called_once_with()


def test_imdb_search_respects_source_and_returns_generic_identity(
    imdb_module: ImdbModule,
) -> None:
    """IMDb 搜索只响应选中的来源，并返回来源与原生 ID 对。"""
    meta = MetaBase("Breaking Bad")
    meta.name = "Breaking Bad"
    meta.type = MediaType.TV

    assert imdb_module.search_medias(meta, media_source=MediaSource.Douban) is None

    results = imdb_module.search_medias(meta, media_source=MediaSource.IMDb)

    assert results and len(results) == 1
    assert results[0].media_source == MediaSource.IMDb
    assert results[0].media_id == "tt0903747"
    assert results[0].imdb_id == "tt0903747"
    assert results[0].type == MediaType.TV


def test_imdb_explicit_identity_recognition_enriches_media_info(
    imdb_module: ImdbModule,
) -> None:
    """显式 IMDb ID 识别应补齐别名、演职员、剧集和图片。"""
    result = imdb_module.recognize_media(
        media_source=MediaSource.IMDb,
        media_id="TT0903747",
    )

    assert isinstance(result, MediaInfo)
    assert result.media_source == MediaSource.IMDb
    assert result.media_id == "tt0903747"
    assert result.title == "Breaking Bad"
    assert result.names == ["Breaking Bad", "绝命毒师"]
    assert result.backdrop_path == "https://image.example/backdrop.jpg"
    assert result.directors[0]["name"] == "Vince Gilligan"
    assert result.actors[0]["character"] == "Walter White"
    assert result.seasons == {1: [1]}
    assert result.season_years == {1: "2008"}
    assert result.number_of_episodes == 1
    assert result.runtime == 47
    assert result.category == "欧美剧"


def test_imdb_name_recognition_requires_imdb_selection(
    imdb_module: ImdbModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名称识别仅在请求级或全局来源选中 IMDb 时执行。"""
    meta = MetaBase("绝命毒师")
    meta.name = "绝命毒师"
    meta.cn_name = "绝命毒师"
    meta.type = MediaType.TV
    monkeypatch.setattr(settings, "RECOGNIZE_SOURCE", MediaSource.TMDB.value)

    assert imdb_module.recognize_media(meta=meta) is None

    result = imdb_module.recognize_media(
        meta=meta,
        media_source=MediaSource.IMDb,
    )

    assert result and result.media_id == "tt0903747"
    imdb_module.imdb_api.search_titles.assert_called_with("绝命毒师")


def test_imdb_recognition_rejects_invalid_or_music_identity(
    imdb_module: ImdbModule,
) -> None:
    """IMDb Module 不应接管非法 ID 或音乐识别请求。"""
    assert (
        imdb_module.recognize_media(
            media_source=MediaSource.IMDb,
            media_id="0903747",
        )
        is None
    )
    assert (
        imdb_module.recognize_media(
            mtype=MediaType.MUSIC,
            media_source=MediaSource.IMDb,
            media_id="tt0903747",
        )
        is None
    )


def test_async_imdb_recognition_uses_async_detail_pipeline(
    imdb_module: ImdbModule,
) -> None:
    """异步显式识别应使用异步详情管线并保持统一身份。"""
    result = asyncio.run(
        imdb_module.async_recognize_media(
            media_source=MediaSource.IMDb,
            media_id="tt0903747",
        )
    )

    assert result and result.media_source == MediaSource.IMDb
    assert result.media_id == "tt0903747"
    assert result.backdrop_path == "https://image.example/backdrop.jpg"
    imdb_module.imdb_api.async_get_title.assert_awaited()
    imdb_module.imdb_api.async_list_credits.assert_awaited_once_with("tt0903747")


def test_imdb_scraping_only_handles_imdb_source(
    imdb_module: ImdbModule,
) -> None:
    """IMDb NFO 和图片刮削只响应显式 IMDb 刮削来源。"""
    media_info = MediaInfo()
    media_info.scrape_source = MediaSource.Douban.value
    assert imdb_module.metadata_nfo(media_info) is None
    assert imdb_module.metadata_img(media_info) is None

    media_info.scrape_source = MediaSource.IMDb.value
    imdb_module.scraper.get_metadata_nfo.return_value = "<movie />"
    imdb_module.scraper.get_metadata_img.return_value = {"poster.jpg": "url"}

    assert imdb_module.metadata_nfo(media_info) == "<movie />"
    assert imdb_module.metadata_img(media_info) == {"poster.jpg": "url"}
