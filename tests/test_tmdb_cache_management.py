import asyncio
import inspect
import pickle
from unittest.mock import Mock

from app.api.endpoints import tmdb as tmdb_endpoint
from app.db.user_oper import get_current_active_superuser_async
from app.modules.themoviedb import tmdb_cache as tmdb_cache_module
from app.modules.themoviedb.tmdb_cache import TmdbCache
from app.schemas.types import MediaType, SystemConfigKey


class _MemoryCacheStub:
    """提供 TMDB 缓存管理测试所需的最小内存后端。"""

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

    def set(self, key: str, value, ttl=None):
        """写入指定缓存条目。"""
        self.data[key] = value

    def clear(self):
        """清空全部缓存条目。"""
        self.data.clear()


class _FileCacheStub:
    """提供 TMDB 持久化测试所需的统一文件缓存替身。"""

    def __init__(self, content: bytes = None):
        """使用预置序列化内容初始化文件缓存。"""
        self.content = content
        self.set_calls = []
        self.delete_calls = []

    def get(self, key: str, region: str):
        """读取预置缓存内容。"""
        return self.content

    def set(self, key: str, value: bytes, region: str):
        """记录统一文件缓存写入。"""
        self.content = value
        self.set_calls.append((key, region))

    def delete(self, key: str, region: str):
        """记录统一文件缓存删除。"""
        self.content = None
        self.delete_calls.append((key, region))


class _TTLCacheStub(_MemoryCacheStub):
    """记录每条数据恢复时剩余 TTL 的内存缓存替身。"""

    def __init__(self):
        """初始化空缓存和 TTL 记录。"""
        super().__init__({})
        self.ttls = {}

    @staticmethod
    def is_redis() -> bool:
        """测试替身固定使用非 Redis 后端。"""
        return False

    def set(self, key: str, value, ttl=None):
        """写入缓存并记录本次设置的 TTL。"""
        super().set(key, value, ttl=ttl)
        self.ttls[key] = ttl


def _build_tmdb_cache(data: dict) -> TmdbCache:
    """构造绕过单例初始化的 TMDB 缓存测试实例。"""
    cache = object.__new__(TmdbCache)
    cache._cache = _MemoryCacheStub(data)
    cache._expires_at = {key: float("inf") for key in data}
    cache._dirty = False
    cache._file_cache = None
    cache._legacy_file_cache = None
    cache._legacy_cache_found = False
    cache.save = lambda force=False: None
    return cache


def _build_initialized_tmdb_cache(monkeypatch, file_cache: _FileCacheStub,
                                  runtime_cache: _TTLCacheStub,
                                  now: float = 1000) -> TmdbCache:
    """使用可控时间和缓存替身初始化完整 TMDB 缓存实例。"""
    monkeypatch.setattr(tmdb_cache_module, "time", lambda: now)
    monkeypatch.setattr(tmdb_cache_module, "TTLCache", lambda **kwargs: runtime_cache)
    monkeypatch.setattr(tmdb_cache_module, "FileCache", lambda **kwargs: file_cache)
    cache = object.__new__(TmdbCache)
    cache.__init__()
    return cache


def test_tmdb_cache_management_endpoints_require_superuser():
    """识别缓存管理接口必须仅允许超级管理员访问。"""
    endpoints = [
        tmdb_endpoint.tmdb_recognition_cache,
        tmdb_endpoint.delete_tmdb_recognition_cache,
        tmdb_endpoint.clear_tmdb_recognition_cache,
    ]

    for endpoint in endpoints:
        dependency = inspect.signature(endpoint).parameters["_"].default.dependency
        assert dependency is get_current_active_superuser_async


def test_tmdb_cache_list_items_normalizes_media_type_and_sorting():
    """管理列表应输出稳定顺序和前端可识别的媒体类型。"""
    cache = _build_tmdb_cache({
        "[电视剧][zh-CN]Zulu-2024-1": {
            "id": 2,
            "title": "Zulu",
            "type": MediaType.TV,
            "year": "2024",
        },
        "[电影][zh-CN]Alpha-2023-None": {
            "id": 1,
            "title": "Alpha",
            "type": "电影",
            "year": "2023",
        },
        "[电影][zh-CN]Missing-2022-None": {"id": 0},
    })

    items = cache.list_items()

    assert [item["title"] for item in items] == ["Alpha", "", "Zulu"]
    assert [item["media_type"] for item in items] == ["movie", "unknown", "tv"]
    assert items[1]["tmdb_id"] == 0


def test_tmdb_cache_delete_and_clear_persist_immediately(monkeypatch):
    """管理操作应修改运行时缓存并立即触发本地持久化。"""
    cache = _build_tmdb_cache({"first": {"id": 1}, "second": {"id": 2}})
    saved_forces = []
    monkeypatch.setattr(cache, "save", lambda force=False: saved_forces.append(force))

    assert cache.delete("first") == {"id": 1}
    assert cache.delete("missing") == {}
    cache.clear()

    assert cache.list_items() == []
    assert saved_forces == [True, True]


def test_tmdb_cache_restores_only_unexpired_persisted_items(monkeypatch):
    """TMDB 持久化恢复应保留每条数据原有期限并跳过已过期条目。"""
    payload = {
        "version": tmdb_cache_module.PERSISTENCE_VERSION,
        "items": {
            "fresh": {
                "value": {"id": 1, "title": "有效"},
                "expires_at": 1030,
            },
            "expired": {
                "value": {"id": 2, "title": "过期"},
                "expires_at": 999,
            },
        },
    }
    file_cache = _FileCacheStub(pickle.dumps(payload))
    runtime_cache = _TTLCacheStub()

    cache = _build_initialized_tmdb_cache(
        monkeypatch=monkeypatch,
        file_cache=file_cache,
        runtime_cache=runtime_cache,
    )

    assert runtime_cache.data == {"fresh": {"id": 1, "title": "有效"}}
    assert runtime_cache.ttls == {"fresh": 30}
    assert cache._expires_at == {"fresh": 1030}
    assert cache._dirty is True


def test_tmdb_cache_persists_individual_expiration_with_file_cache(monkeypatch):
    """TMDB 持久化应通过统一文件缓存保存每条数据的独立过期时间。"""
    file_cache = _FileCacheStub()
    runtime_cache = _TTLCacheStub()
    cache = _build_initialized_tmdb_cache(
        monkeypatch=monkeypatch,
        file_cache=file_cache,
        runtime_cache=runtime_cache,
    )
    runtime_cache.data = {
        "recognized": {"id": 1, "title": "有效"},
        "unrecognized": {"id": 0},
    }
    cache._expires_at = {
        "recognized": 1060,
        "unrecognized": 1070,
    }
    cache._dirty = True

    cache.save()

    payload = pickle.loads(file_cache.content)
    assert file_cache.set_calls == [(
        tmdb_cache_module.PERSISTENCE_KEY,
        tmdb_cache_module.PERSISTENCE_REGION,
    )]
    assert payload == {
        "version": tmdb_cache_module.PERSISTENCE_VERSION,
        "items": {
            "recognized": {
                "value": {"id": 1, "title": "有效"},
                "expires_at": 1060,
            },
        },
    }


def test_tmdb_cache_migrates_legacy_file_to_global_file_cache(monkeypatch):
    """旧 TMDB 缓存应迁移到全局文件缓存并删除旧文件。"""
    primary_cache = _FileCacheStub()
    legacy_cache = _FileCacheStub(pickle.dumps({
        "legacy": {"id": 1, "title": "旧缓存"},
    }))
    file_caches = iter([primary_cache, legacy_cache])
    file_cache_calls = []

    def build_file_cache(**kwargs):
        """记录全局文件缓存构造参数并返回对应替身。"""
        file_cache_calls.append(kwargs)
        return next(file_caches)

    runtime_cache = _TTLCacheStub()
    monkeypatch.setattr(tmdb_cache_module, "time", lambda: 1000)
    monkeypatch.setattr(
        tmdb_cache_module,
        "TTLCache",
        lambda **kwargs: runtime_cache,
    )
    monkeypatch.setattr(tmdb_cache_module, "FileCache", build_file_cache)

    cache = object.__new__(TmdbCache)
    cache.__init__()
    cache.save()

    assert file_cache_calls == [
        {"base": tmdb_cache_module.settings.CACHE_PATH, "ttl": cache.ttl},
        {"base": tmdb_cache_module.settings.TEMP_PATH.parent, "ttl": cache.ttl},
    ]
    assert runtime_cache.data == {"legacy": {"id": 1, "title": "旧缓存"}}
    assert primary_cache.set_calls == [(
        tmdb_cache_module.PERSISTENCE_KEY,
        tmdb_cache_module.PERSISTENCE_REGION,
    )]
    assert legacy_cache.delete_calls == [(
        cache.region,
        tmdb_cache_module.settings.TEMP_PATH.name,
    )]


def test_tmdb_cache_endpoint_returns_management_statistics(monkeypatch):
    """查询接口应返回识别成功和失败条目的统计。"""
    cache = _build_tmdb_cache({
        "recognized": {"id": 1, "title": "Alpha", "type": MediaType.MOVIE},
        "unrecognized": {"id": 0},
    })
    get_system_config = Mock(return_value=7)
    monkeypatch.setattr(tmdb_endpoint, "TmdbCache", lambda: cache)
    monkeypatch.setattr(
        tmdb_endpoint,
        "SystemConfigOper",
        lambda: type("SystemConfigStub", (), {"get": get_system_config})(),
    )
    monkeypatch.setattr(tmdb_endpoint.settings, "MEDIA_RECOGNIZE_SHARE", True)

    response = asyncio.run(tmdb_endpoint.tmdb_recognition_cache(None))

    assert response.success is True
    assert response.data["count"] == 2
    assert response.data["recognized"] == 1
    assert response.data["unrecognized"] == 1
    assert response.data["shared_recognized"] == 7
    assert response.data["shared_recognize_enabled"] is True
    get_system_config.assert_called_once_with(
        SystemConfigKey.MediaRecognizeShareCount
    )


def test_tmdb_cache_delete_endpoint_reports_missing_item(monkeypatch):
    """删除接口应区分成功删除与缓存不存在。"""
    cache = _build_tmdb_cache({"existing": {"id": 1}})
    monkeypatch.setattr(tmdb_endpoint, "TmdbCache", lambda: cache)

    deleted_response = asyncio.run(
        tmdb_endpoint.delete_tmdb_recognition_cache("existing", None)
    )
    missing_response = asyncio.run(
        tmdb_endpoint.delete_tmdb_recognition_cache("missing", None)
    )

    assert deleted_response.success is True
    assert missing_response.success is False


def test_tmdb_cache_clear_endpoint_removes_all_items(monkeypatch):
    """清空接口应删除全部识别缓存。"""
    cache = _build_tmdb_cache({"existing": {"id": 1}})
    monkeypatch.setattr(tmdb_endpoint, "TmdbCache", lambda: cache)

    response = asyncio.run(tmdb_endpoint.clear_tmdb_recognition_cache(None))

    assert response.success is True
    assert cache.list_items() == []
