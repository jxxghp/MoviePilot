import asyncio
from unittest.mock import AsyncMock, Mock
from xml.dom import minidom

import pytest

from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.helper.scraper import MediaScraperHelper
from app.modules.anilist import AniListModule
from app.modules.anilist.anilist import AniListApi
from app.schemas.types import MediaType


@pytest.fixture
def anilist_info() -> dict:
    """构造不依赖网络的AniList媒体详情。"""
    return {
        "id": 154587,
        "title": {
            "romaji": "Sousou no Frieren",
            "english": "Frieren: Beyond Journey's End",
            "native": "葬送のフリーレン",
        },
        "format": "TV",
        "status": "FINISHED",
        "description": "A <b>journey</b> after the adventure.",
        "startDate": {"year": 2023, "month": 9, "day": 29},
        "endDate": {"year": 2024, "month": 3, "day": 22},
        "episodes": 28,
        "duration": 24,
        "countryOfOrigin": "JP",
        "coverImage": {"extraLarge": "https://img.example/poster.jpg"},
        "bannerImage": "https://img.example/backdrop.png",
        "genres": ["Adventure", "Fantasy"],
        "synonyms": ["Frieren"],
        "averageScore": 91,
        "popularity": 300000,
        "isAdult": False,
        "studios": {"nodes": [{"name": "Madhouse"}]},
        "staff": {
            "edges": [
                {
                    "role": "Director",
                    "node": {
                        "name": {"full": "Keiichiro Saito"},
                        "image": {"large": "https://img.example/director.jpg"},
                        "siteUrl": "https://anilist.co/staff/1",
                    },
                }
            ]
        },
        "characters": {
            "edges": [
                {
                    "node": {"name": {"full": "Frieren"}},
                    "voiceActors": [
                        {
                            "name": {"full": "Atsumi Tanezaki"},
                            "image": {"large": "https://img.example/actor.jpg"},
                            "siteUrl": "https://anilist.co/staff/2",
                        }
                    ],
                }
            ]
        },
        "externalLinks": [
            {"site": "AniDB", "url": "https://anidb.net/anime/17617"}
        ],
    }


def test_anilist_id_recognition_normalizes_media_info(anilist_info: dict) -> None:
    """AniList ID识别应生成可供整理和刮削复用的统一媒体信息。"""
    module = AniListModule()
    module.anilist_api = Mock()
    module.anilist_api.detail.return_value = anilist_info

    media = module.recognize_media(anilistid=154587)

    assert media is not None
    assert media.source == "anilist"
    assert media.anilist_id == 154587
    assert media.anidb_id == 17617
    assert media.type == MediaType.TV
    assert media.year == "2023"
    assert media.number_of_episodes == 28
    assert media.seasons[1] == list(range(1, 29))
    assert media.genres == [
        {"id": "Adventure", "name": "Adventure"},
        {"id": "Fantasy", "name": "Fantasy"},
    ]
    assert media.production_companies == [{"name": "Madhouse"}]
    assert media.directors[0]["name"] == "Keiichiro Saito"
    assert media.actors[0]["character"] == "Frieren"
    module.anilist_api.detail.assert_called_once_with(154587)


def test_anilist_title_recognition_respects_request_source(anilist_info: dict) -> None:
    """标题识别仅在本次请求明确选择AniList时使用AniList候选项。"""
    module = AniListModule()
    module.anilist_api = Mock()
    module.anilist_api.search.return_value = [anilist_info]
    meta = MetaBase("Frieren")
    meta.cn_name = "Frieren"
    meta.type = MediaType.TV
    meta.year = "2023"

    media = module.recognize_media(meta=meta, source="anilist")
    skipped = module.recognize_media(meta=meta, source="douban")

    assert media is not None
    assert media.anilist_id == 154587
    assert skipped is None
    module.anilist_api.search.assert_called_once_with("Frieren")


def test_async_anilist_title_recognition(anilist_info: dict) -> None:
    """异步AniList标题识别应与同步结果保持一致。"""
    module = AniListModule()
    module.anilist_api = Mock()
    module.anilist_api.async_search = AsyncMock(return_value=[anilist_info])
    meta = MetaBase("Frieren")
    meta.cn_name = "Frieren"
    meta.type = MediaType.TV

    media = asyncio.run(
        module.async_recognize_media(meta=meta, source="anilist")
    )

    assert media is not None
    assert media.anilist_id == 154587
    module.anilist_api.async_search.assert_awaited_once_with("Frieren")


def test_anilist_scraper_generates_nfo_and_images(anilist_info: dict) -> None:
    """AniList媒体信息应生成带来源ID的NFO以及主海报和背景图。"""
    module = AniListModule()
    module.scraper = MediaScraperHelper()
    media = MediaInfo(anilist_info=anilist_info)
    media.scrape_source = "anilist"

    nfo = module.metadata_nfo(media)
    images = module.metadata_img(media)
    document = minidom.parseString(nfo)
    unique_id = document.getElementsByTagName("uniqueid")[0]

    assert document.documentElement.tagName == "tvshow"
    assert unique_id.firstChild.data == "154587"
    assert unique_id.getAttribute("type") == "anilist"
    assert images == {
        "poster.jpg": "https://img.example/poster.jpg",
        "backdrop.png": "https://img.example/backdrop.png",
    }


def test_anilist_api_extracts_graphql_errors_without_network() -> None:
    """AniList客户端应把GraphQL错误响应统一视为无结果。"""
    response = Mock(status_code=200)
    response.json.return_value = {"errors": [{"message": "invalid"}]}

    assert AniListApi._extract_response(response) is None
