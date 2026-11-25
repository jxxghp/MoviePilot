"""
大集合界面的窗口、轻量投影与旧调用兼容回归。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.endpoints import site, torrent
from app.domain.context import Context, TorrentInfo
from app.foundation.crypto import HashUtils
from app.schemas.site import SiteUserData


def test_site_history_keeps_only_latest_seeding_without_mutating_records():
    """
    历史趋势保留统计量，只对最新一条返回明细，且不污染后续完整查询。
    """
    records = [
        SiteUserData(updated_day=day, upload=index, seeding_info=[(index, 1024)])
        for index, day in enumerate(["2026-10-24", "2026-10-26", "2026-10-25"])
    ]
    query = Mock()
    query.get = AsyncMock(return_value=SimpleNamespace(domain="example.test"))
    query.userdata = AsyncMock(return_value=records)
    query.count_userdata = AsyncMock(return_value=len(records))

    result = asyncio.run(site.read_userdata(1, response=Response(), query=query, page=1, count=90, latest_seeding_only=True))

    query.userdata.assert_awaited_once_with("example.test", None, page=1, count=90)
    assert [record.upload for record in result.data] == [0, 1, 2]
    assert [record.seeding_info for record in result.data] == [[], [(1, 1024)], []]
    assert [record.seeding_info for record in records] == [[(0, 1024)], [(1, 1024)], [(2, 1024)]]
    complete = asyncio.run(site.read_userdata(1, response=Response(), query=query))
    assert complete.data == records


def test_cache_window_limits_hashing_and_preserves_full_description_identity():
    """
    大缓存只为当前页计算摘要，短描述不改变删除和重识别使用的身份。
    """
    contexts = [
        Context(torrent_info=TorrentInfo(title=f"Movie {index}", description="长描述" * 1000, site_name="站点甲"))
        for index in range(1000)
    ]
    chain = Mock()
    chain.async_get_torrents = AsyncMock(return_value={"example.test": contexts})
    original_md5 = HashUtils.md5
    with (
        patch.object(torrent, "TorrentsChain", return_value=chain),
        patch.object(
            HashUtils,
            "md5",
            wraps=original_md5,
        ) as md5,
    ):
        result = asyncio.run(torrent.torrents_cache(page=2, count=2, brief=True))

    assert md5.call_count == 2
    assert result.data["count"] == result.data["total"] == 1000
    assert [item["title"] for item in result.data["data"]] == ["Movie 2", "Movie 3"]
    assert len(result.data["data"][0]["description"]) == 200
    assert result.data["data"][0]["hash"] == original_md5("Movie 2" + "长描述" * 1000)
    assert len(contexts[2].torrent_info.description) == 3000


def test_cache_filters_before_pagination_and_preserves_unpaged_contract():
    """
    标题与站点筛选覆盖未加载页，省略窗口时仍返回完整描述与完整结果。
    """
    contexts = [
        Context(torrent_info=TorrentInfo(title=title, description="完整描述", site_name=name))
        for title, name in [("Movie A", "甲"), ("Other", "乙"), ("MOVIE B", "乙")]
    ]
    chain = Mock()
    chain.async_get_torrents = AsyncMock(return_value={"example.test": contexts})
    with patch.object(torrent, "TorrentsChain", return_value=chain):
        result = asyncio.run(torrent.torrents_cache(page=1, count=1, title="movie", site="乙"))
        complete = asyncio.run(torrent.torrents_cache())
        empty = asyncio.run(torrent.torrents_cache(page=5, count=1))

    assert result.data["site_names"] == ["乙", "甲"]
    assert result.data["total"] == 1
    assert result.data["data"][0]["title"] == "MOVIE B"
    assert len(complete.data["data"]) == 3
    assert complete.data["data"][0]["description"] == "完整描述"
    assert empty.data["data"] == []
    assert empty.data["total"] == 3


def test_cache_http_contract_rejects_invalid_windows_and_serializes_totals():
    """
    HTTP 边界校验页码和最大窗口，并保留筛选数量与可选站点元数据。
    """
    app = FastAPI()
    app.include_router(torrent.router, prefix="/torrent")
    app.dependency_overrides[get_current_active_superuser_async] = lambda: Mock()
    chain = Mock()
    chain.async_get_torrents = AsyncMock(return_value={})
    with patch.object(torrent, "TorrentsChain", return_value=chain), TestClient(app) as client:
        assert client.get("/torrent/cache?page=0&count=100").status_code == 422
        assert client.get("/torrent/cache?page=1&count=201").status_code == 422
        response = client.get("/torrent/cache?page=1&count=100&brief=true")
    assert response.status_code == 200
    assert response.json()["data"] == {"count": 0, "sites": 0, "data": [], "total": 0, "site_names": []}
