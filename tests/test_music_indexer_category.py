from app.modules.indexer.spider import (
    SiteSpider,
    resolve_category_media_type,
    select_media_categories,
)
from app.schemas.types import MediaType


def _category_config() -> dict:
    """构造包含电影、电视剧和音乐分类的站点配置。"""
    return {
        "movie": [{"id": "1", "name": "电影"}],
        "tv": [{"id": "2", "name": "电视剧"}],
        "music": [{"id": "3", "name": "音乐"}],
    }


def test_select_media_categories_supports_music():
    """站点搜索限定音乐类型时应只提交音乐分类。"""
    assert select_media_categories(_category_config(), MediaType.MUSIC) == [
        {"id": "3", "name": "音乐"}
    ]


def test_select_media_categories_includes_music_when_type_unspecified():
    """未限定媒体类型的浏览应继续包含全部已配置分类。"""
    assert [item["id"] for item in select_media_categories(_category_config(), None)] == [
        "1",
        "2",
        "3",
    ]


def test_resolve_category_media_type_supports_music():
    """音乐分类 ID 应映射为统一音乐媒体类型。"""
    assert resolve_category_media_type("3", _category_config()) == MediaType.MUSIC


def test_resolve_category_media_type_rejects_ambiguous_category():
    """一个分类同时归属多个媒体类型时应保持未知，避免误整理。"""
    category = _category_config()
    category["movie"].append({"id": "3", "name": "综合"})

    assert resolve_category_media_type("3", category) == MediaType.UNKNOWN


def test_site_level_music_type_fills_missing_torrent_category():
    """音乐专属站点缺少逐条分类字段时应使用站点级音乐类型。"""
    spider = SiteSpider(
        indexer={
            "id": "music",
            "name": "Music",
            "domain": "https://music.example/",
            "media_type": "music",
            "search": {},
            "torrents": {"fields": {}},
        },
        mtype=MediaType.MUSIC,
    )

    spider._SiteSpider__get_category(None)

    assert spider.torrents_info["category"] == MediaType.MUSIC.value
