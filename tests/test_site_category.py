"""
回放构建仓的站点分类配置，验证接口序列化和搜索参数兼容性。
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import site
from app.modules.indexer.spider import SiteSpider
from app.schemas.site import SiteCategory
from app.schemas.types import MediaType


@pytest.fixture
def category_indexers():
    """
    读取构建仓的公开 YAML 快照，来源提交和路径记录在载具中。
    """
    fixture_path = Path(__file__).parent / "fixtures" / "site_categories.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))["indexers"]


@pytest.mark.parametrize(
    ("site_id", "expected_ids"),
    [
        ("hhanclub", [401, 405, 404, 402, 403, 410]),
        ("animez", ["MOVIE", "SPECIAL", "OVA", "ONA", "TV", "TV_SHORT", "MUSIC"]),
        ("starspace", [401, 405, 404, 402, 403, "music"]),
        ("anibt", ["anime", "raw", "music", "stage"]),
        (
            "totheglory",
            [
                "电影DVDRip", "电影720p", "电影1080i/p", "BluRay原盘", "影视2160p", "UHD原盘",
                "高清动漫", "动漫原盘", "纪录片720p", "纪录片1080i/p", "纪录片BluRay原盘", "影视2160p",
                "欧美剧720p", "欧美剧1080i/p", "高清日剧", "大陆港台剧1080i/p", "大陆港台剧720p",
                "高清韩剧", "欧美剧包", "日剧包", "韩剧包", "华语剧包", "韩国综艺", "日本综艺",
                "高清综艺", "无损音乐FLAC&APE",
            ],
        ),
    ],
)
def test_site_categories_serialize_real_resource_configs(category_indexers, monkeypatch, site_id, expected_ids):
    """
    真实分类经过响应封装后保留数字与文本标识、音乐分组及去重顺序。
    """
    indexer = category_indexers[site_id]
    snapshot = copy.deepcopy(indexer)
    query = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(domain=indexer["domain"])))
    helper = SimpleNamespace(async_get_indexer=AsyncMock(return_value=indexer))
    monkeypatch.setattr(site, "SitesHelper", Mock(return_value=helper))
    app = FastAPI()
    app.include_router(site.router, prefix="/api/v1/site")
    app.dependency_overrides[site.get_site_query_service] = lambda: query
    app.dependency_overrides[site.verify_token] = lambda: None

    with TestClient(app) as client:
        response = client.get("/api/v1/site/category/1")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert [item["id"] for item in body["data"]] == expected_ids
    assert indexer == snapshot


@pytest.mark.parametrize(
    ("category_id", "expected"),
    [(401, 401), ("401", 401), ("00401", 401), ("music", "music"), ("电影720p", "电影720p"), (None, None)],
)
def test_category_schema_preserves_text_and_legacy_numeric_ids(category_id, expected):
    """
    数字字符串沿用旧接口的整数输出，真实文本标识保持原值。
    """
    category = SiteCategory(id=category_id)

    assert category.id == expected
    assert type(category.id) is type(expected)


@pytest.mark.parametrize(
    ("site_id", "media_type", "category_id", "parameter", "expected_value", "path"),
    [
        ("hhanclub", MediaType.MOVIE, "401", "cat[]", "401", "/torrents.php"),
        ("hhanclub", MediaType.MUSIC, "410", "cat[]", "410", "/special.php"),
        ("animez", MediaType.MUSIC, "MUSIC", "format[]", "MUSIC", "/torrents.php"),
        ("totheglory", MediaType.MOVIE, "电影DVDRip", "search_field", "回放测试 分类:电影DVDRip", "/browse.php"),
        (
            "totheglory", MediaType.MUSIC, "无损音乐FLAC&APE", "search_field",
            "回放测试 分类:无损音乐FLAC&APE", "/browse.php",
        ),
    ],
)
def test_resource_categories_keep_real_search_parameters(
    category_indexers, site_id, media_type, category_id, parameter, expected_value, path,
):
    """
    接口返回的数字及文本分类仍能生成站点要求的精确搜索参数。
    """
    spider = SiteSpider(
        indexer=category_indexers[site_id], keyword="回放测试", mtype=media_type, cat=category_id,
    )

    parsed = urlparse(spider._SiteSpider__get_search_url())

    assert parsed.path == path
    assert parse_qs(parsed.query)[parameter] == [expected_value]
