"""
验证站点浏览接口保留零起始页码和多选分类。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.endpoints import site as site_endpoint


@pytest.mark.parametrize("page", [0, 1, 2])
def test_site_resource_preserves_zero_based_pages_and_categories(monkeypatch, page):
    """
    第一页与后续页必须保持独立偏移，多选分类原样交给浏览链路。
    """
    query = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(domain="https://site.example/")))
    torrent = SimpleNamespace(to_dict=lambda: {"title": "命中资源"})
    browse = AsyncMock(return_value=[torrent])
    monkeypatch.setattr(site_endpoint, "TorrentsChain", lambda: SimpleNamespace(async_browse=browse))

    result = asyncio.run(site_endpoint.site_resource(
        site_id=1, keyword="关键词", cat="401,402", page=page, query=query,
    ))

    assert result == [{"title": "命中资源"}]
    query.get.assert_awaited_once_with(1)
    browse.assert_awaited_once_with(
        domain="https://site.example/", keyword="关键词", cat="401,402", page=page, mtype=None,
    )
