import asyncio
from unittest.mock import AsyncMock, Mock
from xml.dom import minidom

import pytest

from app.application.torrent.download import TorrentHelper
from app.chain.search import SearchChain
from app.chain.search.plan import SearchPlanOwner
from app.domain.context import MediaInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.domain.scraper import MediaScraperHelper
from app.modules.anilist import AniListModule
from app.modules.anilist.anilist import AniListApi
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def anilist_info() -> dict:
    """构造不依赖网络的AniList媒体详情。"""
    return {
        "id": 154587,
        "title": {
            "romaji": "Sousou no Frieren",
            "english": "Frieren: Beyond Journey's End",
            "native": "葬送のフリーレン",
            "chinese": "葬送的芙莉莲",
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
        "synonyms": ["葬送的芙莉莲", "Frieren"],
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

    media = module.recognize_media(
        media_source=MediaSource.AniList, media_id="154587"
    )

    assert media is not None
    assert media.media_source == MediaSource.AniList
    assert media.media_id == "154587"
    assert media.anilist_id == 154587
    assert media.title == "葬送的芙莉莲"
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


def test_anilist_sequel_entry_matches_series_numbered_torrent(
    anilist_info: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AniList 独立续季条目应按真实季号和基础别名匹配整部剧编号的资源。"""
    info = {
        **anilist_info,
        "id": 135865,
        "title": {
            "chinese": "幼女戦記Ⅱ",
            "native": "幼女戦記Ⅱ",
            "romaji": "Youjo Senki II",
            "english": "Saga of Tanya the Evil Season 2",
        },
        "synonyms": ["Youjo Senki 2"],
        "startDate": {"year": 2026},
        "episodes": 12,
    }
    module = AniListModule()
    module.anilist_api = Mock()
    module.anilist_api.detail.return_value = info
    media = module.recognize_media(media_source=MediaSource.AniList, media_id="135865")

    assert media is not None
    assert media.season == 2
    assert media.seasons == {2: list(range(1, 13))}
    assert media.season_years == {2: "2026"}
    assert "Youjo Senki" in media.names
    prepared = SearchPlanOwner._prepare_media_input(media)
    season_episodes, _ = SearchPlanOwner._prepare_params(prepared)
    assert prepared.season == 2
    assert season_episodes == {2: []}

    torrent = TorrentInfo(
        site_name="测试站点",
        title="Youjo Senki S02E12 2026 1080p CR WEB-DL x264 AAC-AnimeS@ADWeb",
        category=MediaType.TV.value,
    )
    torrent_meta = MetaInfo(torrent.title)
    assert TorrentHelper.match_season_episodes(torrent, torrent_meta, season_episodes)
    assert TorrentHelper.match_torrent(prepared, torrent_meta, torrent)

    wrong_season = TorrentInfo(
        site_name="测试站点",
        title="Youjo Senki S01E12 2026 1080p WEB-DL",
        category=MediaType.TV.value,
    )
    wrong_meta = MetaInfo(wrong_season.title)
    assert not TorrentHelper.match_season_episodes(wrong_season, wrong_meta, season_episodes)
    assert not TorrentHelper.match_torrent(prepared, wrong_meta, wrong_season)
    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(lambda contexts: contexts))
    contexts = object.__new__(SearchChain)._parse_result(
        torrents=[torrent, wrong_season],
        mediainfo=prepared,
        season_episodes=season_episodes,
        rule_groups=[],
    )
    assert len(contexts) == 1
    assert contexts[0].torrent_info.title == torrent.title


def test_anilist_sequel_without_torrent_year_uses_shared_tmdb_identity() -> None:
    """无年份基础标题需要候选与 AniList 条目共享 TMDB 身份才能消歧。"""
    target = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="135865",
        tmdb_id=69346,
        title="幼女戦記Ⅱ",
        original_title="幼女戦記Ⅱ",
        names=["Youjo Senki", "Youjo Senki II"],
        type=MediaType.TV,
        year="2026",
        season=2,
        seasons={2: list(range(1, 13))},
        season_years={2: "2026"},
    )
    torrent_meta = MetaInfo("Youjo Senki S02E12 1080p WEB-DL")
    matching = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="69346",
        tmdb_id=69346,
        title="幼女战记",
        type=MediaType.TV,
        year="2017",
    )
    conflicting = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="99999",
        tmdb_id=99999,
        title="另一部作品",
        type=MediaType.TV,
        year="2017",
    )

    assert TorrentHelper.requires_identity_disambiguation(target, torrent_meta)
    assert TorrentHelper.match_same_work_evidence(target, matching, torrent_meta)[0]
    assert not TorrentHelper.match_same_work_evidence(target, conflicting, torrent_meta)[0]


def test_anilist_title_recognition_respects_request_source(anilist_info: dict) -> None:
    """标题识别仅在本次请求明确选择AniList时使用AniList候选项。"""
    module = AniListModule()
    module.anilist_api = Mock()
    module.anilist_api.search.return_value = [anilist_info]
    meta = MetaBase("Frieren")
    meta.cn_name = "Frieren"
    meta.type = MediaType.TV
    meta.year = "2023"

    media = module.recognize_media(meta=meta, media_source=MediaSource.AniList)
    skipped = module.recognize_media(meta=meta, media_source=MediaSource.Douban)

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
        module.async_recognize_media(meta=meta, media_source=MediaSource.AniList)
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


def test_anilist_title_falls_back_to_native_language(anilist_info: dict) -> None:
    """anilist-chinese 未注入中文标题时应优先回退原语言标题。"""
    anilist_info["synonyms"] = ["Frieren"]
    anilist_info["title"].pop("chinese")

    media = MediaInfo(anilist_info=anilist_info)

    assert media.title == "葬送のフリーレン"


def test_anilist_title_uses_injected_chinese_synonym_for_latin_title(
    anilist_info: dict,
) -> None:
    """代理主标题仍为拉丁字母时应选择其追加的中文别名。"""
    anilist_info["title"]["chinese"] = "Frieren"
    anilist_info["synonyms"] = ["Official Alias", "葬送的芙莉莲"]

    media = MediaInfo(anilist_info=anilist_info)

    assert media.title == "葬送的芙莉莲"
