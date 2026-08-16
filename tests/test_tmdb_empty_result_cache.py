"""
TMDB request 层空结果快照短 TTL 缓存测试。

TMDB 代理故障期间「合法 JSON 但 results 为空」的响应若随默认 TTL（可达数十小时）
固化，故障自愈后同名搜索仍持续命中空结果。空结果快照必须仍入缓存（拦住故障窗口
内的重复回源），但改用独立的 30 分钟短 TTL，过期后自然恢复回源。
"""
import asyncio
from unittest.mock import patch

from app.modules.themoviedb.tmdbv3api.tmdb import (
    TMDb,
    _is_empty_result_snapshot,
)
from app.runtime.cache import MemoryBackend
from app.runtime.config import settings

from tests.test_tmdb_response_cache import _FakeResponse

EMPTY_PAYLOAD = {"page": 1, "results": [], "total_results": 0, "total_pages": 0}
NOT_EMPTY_PAYLOAD = {"page": 1, "results": [{"id": 1}], "total_results": 1, "total_pages": 1}
HEADERS = {"Content-Type": "application/json"}


def _snapshot(payload: dict) -> dict:
    """构造一个带快照标记的响应结构。"""
    return {TMDb._RESPONSE_SNAPSHOT_MARKER: True, "headers": {}, "json": payload}


def _request_region_cache():
    """取出 TMDb.request 装饰器使用的内存缓存区实例。"""
    return MemoryBackend._region_caches[MemoryBackend.get_region(TMDb.request.cache_region)]


def _make_tmdb() -> TMDb:
    """构造带测试 API Key 的 TMDb 客户端。"""
    tmdb = TMDb()
    tmdb.api_key = "test-key"
    return tmdb


def test_empty_result_cache_ttl_is_thirty_minutes():
    """空结果缓存的独立过期时间应为 30 分钟。"""
    assert settings.EMPTY_RESULT_CACHE_TTL == 30 * 60


def test_empty_result_snapshot_predicate():
    """空结果谓词只认 results 为空列表的快照，详情与有结果的响应不算空。"""
    assert _is_empty_result_snapshot(_snapshot(EMPTY_PAYLOAD))
    assert not _is_empty_result_snapshot(_snapshot(NOT_EMPTY_PAYLOAD))
    assert not _is_empty_result_snapshot(_snapshot({"id": 98865, "title": "Test"}))
    # json 非字典或无 results 字段时不能误判为空结果
    assert not _is_empty_result_snapshot(_snapshot("upstream error"))
    assert not _is_empty_result_snapshot(None)


def test_empty_result_is_cached_but_expires_with_short_ttl():
    """空结果快照仍入缓存避免重复回源，但按短 TTL 过期后恢复回源。"""
    tmdb = _make_tmdb()
    url = "https://api.tmdb.test/empty-short-ttl"
    fake = _FakeResponse(EMPTY_PAYLOAD, HEADERS)

    with patch.object(TMDb, "_request_once", return_value=fake) as req:
        tmdb.request("GET", url, None, None)
        tmdb.request("GET", url, None, None)
    assert req.call_count == 1

    region_cache = _request_region_cache()
    started_at = region_cache.timer()
    region_cache.expire(time=started_at + settings.EMPTY_RESULT_CACHE_TTL + 1)

    with patch.object(TMDb, "_request_once", return_value=fake) as req:
        tmdb.request("GET", url, None, None)
    assert req.call_count == 1


def test_non_empty_result_keeps_default_ttl():
    """有结果的响应不受短 TTL 影响，过期点仍为默认元数据缓存 TTL 之后。"""
    tmdb = _make_tmdb()
    url = "https://api.tmdb.test/non-empty-default-ttl"
    fake = _FakeResponse(NOT_EMPTY_PAYLOAD, HEADERS)

    with patch.object(TMDb, "_request_once", return_value=fake) as req:
        tmdb.request("GET", url, None, None)
    assert req.call_count == 1

    region_cache = _request_region_cache()
    started_at = region_cache.timer()
    # 推进到短 TTL 之后：非空结果不应在此刻过期
    region_cache.expire(time=started_at + settings.EMPTY_RESULT_CACHE_TTL + 1)

    tmdb.request("GET", url, None, None)
    assert req.call_count == 1


def test_async_empty_result_is_cached_with_short_ttl():
    """异步请求的空结果快照与同步路径一致，入缓存但按短 TTL 过期。"""
    tmdb = _make_tmdb()
    url = "https://api.tmdb.test/async-empty-short-ttl"
    fake = _FakeResponse(EMPTY_PAYLOAD, HEADERS)

    with patch.object(TMDb, "_async_request_once", return_value=fake) as req:
        asyncio.run(tmdb.async_request("GET", url, None, None))
        asyncio.run(tmdb.async_request("GET", url, None, None))
    assert req.call_count == 1

    region_cache = _request_region_cache()
    started_at = region_cache.timer()
    region_cache.expire(time=started_at + settings.EMPTY_RESULT_CACHE_TTL + 1)

    with patch.object(TMDb, "_async_request_once", return_value=fake) as req:
        asyncio.run(tmdb.async_request("GET", url, None, None))
    assert req.call_count == 1
