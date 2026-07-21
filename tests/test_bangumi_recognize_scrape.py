from unittest.mock import Mock
from xml.dom import minidom

from app.core.meta import MetaBase
from app.helper.scraper import MediaScraperHelper
from app.modules.bangumi import BangumiModule
from app.schemas.types import MediaType


def _bangumi_info() -> dict:
    """构造Bangumi识别与刮削测试详情。"""
    return {
        "id": 400602,
        "name": "Sousou no Frieren",
        "name_cn": "葬送的芙莉莲",
        "platform": "TV",
        "date": "2023-09-29",
        "eps": 28,
        "summary": "勇者一行击败魔王后的故事。",
        "images": {"large": "https://lain.example/poster.jpg"},
        "rating": {"score": 8.9},
        "tags": [{"name": "奇幻"}, {"name": "冒险"}],
        "infobox": [
            {"key": "动画制作", "value": "MADHOUSE"},
            {"key": "导演", "value": [{"v": "斋藤圭一郎"}]},
        ],
    }


def test_bangumi_title_recognition_loads_detail_and_people() -> None:
    """Bangumi标题识别应搜索候选、读取详情并补齐演职员。"""
    module = BangumiModule()
    module.bangumiapi = Mock()
    module.bangumiapi.search.return_value = [
        {"id": 400602, "name": "Sousou no Frieren", "name_cn": "葬送的芙莉莲"}
    ]
    module.bangumiapi.detail.return_value = _bangumi_info()
    module.bangumiapi.credits.return_value = [
        {"name": "种崎敦美", "career": ["芙莉莲"]}
    ]
    meta = MetaBase("葬送的芙莉莲")
    meta.cn_name = "葬送的芙莉莲"
    meta.type = MediaType.TV
    meta.year = "2023"

    media = module.recognize_media(meta=meta, source="bangumi")

    assert media is not None
    assert media.source == "bangumi"
    assert media.bangumi_id == 400602
    assert media.number_of_episodes == 28
    assert media.genres == [
        {"id": "奇幻", "name": "奇幻"},
        {"id": "冒险", "name": "冒险"},
    ]
    assert media.production_companies == [{"name": "MADHOUSE"}]
    assert media.directors == [{"name": "斋藤圭一郎"}]
    assert media.actors[0]["name"] == "种崎敦美"


def test_bangumi_scraper_generates_source_nfo() -> None:
    """Bangumi来源应可生成带Bangumi唯一ID的NFO与图片清单。"""
    module = BangumiModule()
    module.bangumiapi = Mock()
    module.scraper = MediaScraperHelper()
    module.bangumiapi.detail.return_value = _bangumi_info()
    module.bangumiapi.credits.return_value = []
    media = module.recognize_media(bangumiid=400602)
    media.scrape_source = "bangumi"

    nfo = module.metadata_nfo(media)
    images = module.metadata_img(media)
    document = minidom.parseString(nfo)
    unique_id = document.getElementsByTagName("uniqueid")[0]

    assert unique_id.firstChild.data == "400602"
    assert unique_id.getAttribute("type") == "bangumi"
    assert images == {"poster.jpg": "https://lain.example/poster.jpg"}
