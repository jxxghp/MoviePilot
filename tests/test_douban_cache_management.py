import asyncio
import inspect

from app.api.endpoints import douban as douban_endpoint
from app.db.user_oper import get_current_active_superuser_async
from app.modules.douban.douban_cache import DoubanCache
from app.schemas.types import MediaType


class _MemoryCacheStub:
    """提供豆瓣缓存管理测试所需的最小内存后端。"""

    def __init__(self, data: dict):
        """使用给定字典初始化测试缓存。"""
        self.data = data

    def items(self):
        """返回全部缓存条目。"""
        return self.data.items()

    def get(self, key: str):
        """读取指定缓存条目。"""
        return self.data.get(key)

    def delete(self, key: str):
        """删除指定缓存条目。"""
        self.data.pop(key, None)

    def clear(self):
        """清空全部缓存条目。"""
        self.data.clear()


def _build_douban_cache(data: dict) -> DoubanCache:
    """构造绕过单例初始化的豆瓣缓存测试实例。"""
    cache = object.__new__(DoubanCache)
    cache._cache = _MemoryCacheStub(data)
    cache.save = lambda force=False: None
    return cache


def test_douban_cache_management_endpoints_require_superuser():
    """豆瓣识别缓存管理接口必须仅允许超级管理员访问。"""
    endpoints = [
        douban_endpoint.douban_recognition_cache,
        douban_endpoint.delete_douban_recognition_cache,
        douban_endpoint.clear_douban_recognition_cache,
    ]

    for endpoint in endpoints:
        dependency = inspect.signature(endpoint).parameters["_"].default.dependency
        assert dependency is get_current_active_superuser_async


def test_douban_cache_list_items_normalizes_media_type_and_sorting():
    """豆瓣管理列表应输出稳定顺序和前端可识别的媒体类型。"""
    cache = _build_douban_cache({
        "[电视剧]Zulu-2024-1": {
            "id": "2",
            "title": "Zulu",
            "type": MediaType.TV,
            "year": "2024",
        },
        "[电影]Alpha-2023-None": {
            "id": "1",
            "title": "Alpha",
            "type": "电影",
            "year": "2023",
            "poster_path": "https://example.com/alpha.jpg",
        },
        "[电影]Missing-2022-None": {"id": 0},
    })

    items = cache.list_items()

    assert [item["title"] for item in items] == ["Alpha", "", "Zulu"]
    assert [item["media_type"] for item in items] == ["movie", "unknown", "tv"]
    assert items[0]["poster_path"] == "https://example.com/alpha.jpg"
    assert items[1]["douban_id"] == 0


def test_douban_cache_delete_and_clear_persist_immediately(monkeypatch):
    """豆瓣管理操作应修改运行时缓存并立即触发本地持久化。"""
    cache = _build_douban_cache({"first": {"id": "1"}, "second": {"id": "2"}})
    saved_forces = []
    monkeypatch.setattr(cache, "save", lambda force=False: saved_forces.append(force))

    assert cache.delete("first") == {"id": "1"}
    assert cache.delete("missing") == {}
    cache.clear()

    assert cache.list_items() == []
    assert saved_forces == [True, True]


def test_douban_cache_endpoint_returns_management_statistics(monkeypatch):
    """豆瓣查询接口应返回识别成功和失败条目的统计。"""
    cache = _build_douban_cache({
        "recognized": {"id": "1", "title": "Alpha", "type": MediaType.MOVIE},
        "unrecognized": {"id": 0},
    })
    monkeypatch.setattr(douban_endpoint, "DoubanCache", lambda: cache)

    response = asyncio.run(douban_endpoint.douban_recognition_cache(None))

    assert response.success is True
    assert response.data["count"] == 2
    assert response.data["recognized"] == 1
    assert response.data["unrecognized"] == 1


def test_douban_cache_delete_endpoint_reports_missing_item(monkeypatch):
    """豆瓣删除接口应区分成功删除与缓存不存在。"""
    cache = _build_douban_cache({"existing": {"id": "1"}})
    monkeypatch.setattr(douban_endpoint, "DoubanCache", lambda: cache)

    deleted_response = asyncio.run(
        douban_endpoint.delete_douban_recognition_cache("existing", None)
    )
    missing_response = asyncio.run(
        douban_endpoint.delete_douban_recognition_cache("missing", None)
    )

    assert deleted_response.success is True
    assert missing_response.success is False


def test_douban_cache_clear_endpoint_removes_all_items(monkeypatch):
    """豆瓣清空接口应删除全部识别缓存。"""
    cache = _build_douban_cache({"existing": {"id": "1"}})
    monkeypatch.setattr(douban_endpoint, "DoubanCache", lambda: cache)

    response = asyncio.run(douban_endpoint.clear_douban_recognition_cache(None))

    assert response.success is True
    assert cache.list_items() == []
