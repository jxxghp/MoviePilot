import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.endpoints import site as site_endpoint
from app.schemas.types import MediaType


@pytest.mark.parametrize(
    ("indexer", "media_type", "expected"),
    [
        ({"media_type": "music"}, MediaType.MUSIC, True),
        ({"media_type": "music"}, MediaType.MOVIE, False),
        ({"category": {"music": [{"id": "3"}]}}, MediaType.MUSIC, True),
        ({"category": {"music": [{"id": "3"}]}}, MediaType.TV, False),
        ({"category": {"movie": [{"id": "1"}]}}, MediaType.MOVIE, True),
        ({"category": {}}, MediaType.MOVIE, True),
        ({"category": {}}, MediaType.MUSIC, False),
    ],
)
def test_indexer_supports_requested_media_type(indexer, media_type, expected):
    """站点媒体声明和分类配置应生成正确的媒体类型兼容结果。"""
    assert site_endpoint._indexer_supports_media_type(indexer, media_type) is expected


@pytest.mark.parametrize(
    ("media_type", "expected_ids"),
    [
        ("music", [2, 3]),
        ("movie", [1, 3, 4]),
        ("tv", [4]),
    ],
)
def test_read_sites_by_media_type_filters_configured_active_sites(monkeypatch, media_type, expected_ids):
    """按媒体类型查询时应保留兼容启用站点，并维持数据库优先级顺序。"""
    sites = [
        SimpleNamespace(id=1, domain="movie.example", is_active=True),
        SimpleNamespace(id=2, domain="music.example", is_active=True),
        SimpleNamespace(id=3, domain="mixed.example", is_active=True),
        SimpleNamespace(id=4, domain="generic.example", is_active=True),
        SimpleNamespace(id=5, domain="inactive.example", is_active=False),
    ]
    indexers = [
        {"id": 1, "category": {"movie": [{"id": "1"}]}},
        {"id": 2, "media_type": "music"},
        {"id": 3, "category": {"movie": [{"id": "1"}], "music": [{"id": "3"}]}},
        {"id": 4, "category": {}},
        {"id": 5, "media_type": "music"},
    ]
    list_sites = AsyncMock(return_value=sites)
    get_indexers = AsyncMock(return_value=indexers)
    monkeypatch.setattr(
        site_endpoint,
        "SitesHelper",
        lambda: SimpleNamespace(async_get_indexers=get_indexers),
    )

    result = asyncio.run(
        site_endpoint.read_sites_by_media_type(
            media_type,
            query=SimpleNamespace(list_ordered=list_sites),
        )
    )

    assert [site.id for site in result] == expected_ids
    list_sites.assert_awaited_once()
    get_indexers.assert_awaited_once()


def test_read_sites_by_media_type_rejects_unknown_type():
    """未知媒体类型应返回明确的客户端参数错误。"""
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            site_endpoint.read_sites_by_media_type(
                "podcast",
                query=SimpleNamespace(list_ordered=AsyncMock()),
            )
        )

    assert error.value.status_code == 400
    assert error.value.detail == "不支持的媒体类型"


def test_site_mapping_uses_async_query_port():
    """异步站点映射不得在事件循环内调用同步数据库查询。"""
    sites = [
        SimpleNamespace(domain="one.example", name="One"),
        SimpleNamespace(domain="two.example", name="Two"),
    ]
    query = SimpleNamespace(
        list_ordered=AsyncMock(return_value=sites),
        list_sync=pytest.fail,
    )

    result = asyncio.run(site_endpoint.site_mapping(query=query))

    assert result.success is True
    assert result.data == {
        "one.example": "One",
        "two.example": "Two",
    }
    query.list_ordered.assert_awaited_once()
