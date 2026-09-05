"""MusicBrainz 音乐识别本地持久化缓存测试。

覆盖缓存键生成、读写回环、负缓存、持久化恢复与保存、管理端点
统计与权限，以及 MusicBrainzModule 识别流程对缓存的命中与回填。
"""
import asyncio
import inspect
import pickle
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.endpoints import music as music_endpoint
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.api.deps import get_current_active_superuser_async
from app.modules.musicbrainz import cache as music_cache_module
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.musicbrainz.cache import MusicBrainzCache


class _MemoryCacheStub:
    """提供音乐缓存管理测试所需的最小内存后端。"""

    def __init__(self, data: dict):
        """使用给定字典初始化测试缓存。"""
        self.data = data

    @staticmethod
    def is_redis() -> bool:
        """测试替身固定使用非 Redis 后端。"""
        return False

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


class _TTLCacheStub(_MemoryCacheStub):
    """记录每条数据恢复时剩余 TTL 的内存缓存替身。"""

    def __init__(self):
        """初始化空缓存和 TTL 记录。"""
        super().__init__({})
        self.ttls = {}

    def set(self, key: str, value, ttl=None):
        """写入缓存并记录本次设置的 TTL。"""
        super().set(key, value, ttl=ttl)
        self.ttls[key] = ttl


class _FileCacheStub:
    """提供音乐缓存持久化测试所需的统一文件缓存替身。"""

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


def _build_music_cache(data: dict) -> MusicBrainzCache:
    """构造绕过单例初始化的音乐识别缓存测试实例。"""
    cache = object.__new__(MusicBrainzCache)
    cache.maxsize = 256
    cache.ttl = 3600
    cache.region = "__musicbrainz_cache__"
    cache._cache = _MemoryCacheStub(data)
    cache._expires_at = {key: float("inf") for key in data}
    cache._dirty = False
    cache._file_cache = None
    cache.save = lambda force=False: None
    return cache


def _build_initialized_music_cache(monkeypatch, file_cache: _FileCacheStub,
                                   runtime_cache: _TTLCacheStub,
                                   now: float = 1000) -> MusicBrainzCache:
    """使用可控时间和缓存替身初始化完整音乐识别缓存实例。"""
    monkeypatch.setattr(music_cache_module, "time", lambda: now)
    monkeypatch.setattr(music_cache_module, "TTLCache", lambda **kwargs: runtime_cache)
    monkeypatch.setattr(music_cache_module, "FileCache", lambda **kwargs: file_cache)
    cache = object.__new__(MusicBrainzCache)
    cache.__init__()
    return cache


def _music_info(**kwargs) -> MusicInfo:
    """构造标准音乐识别结果，默认携带远端身份。"""
    defaults = {
        "media_source": "musicbrainz",
        "media_id": "rec-1",
        "title": "晴天",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "year": 2003,
    }
    defaults.update(kwargs)
    return MusicInfo(**defaults)


def test_music_cache_endpoints_require_superuser():
    """音乐识别缓存管理接口必须仅允许超级管理员访问。"""
    endpoints = [
        music_endpoint.music_recognition_cache,
        music_endpoint.delete_music_recognition_cache,
        music_endpoint.clear_music_recognition_cache,
    ]

    for endpoint in endpoints:
        dependency = inspect.signature(endpoint).parameters["_"].default.dependency
        assert dependency is get_current_active_superuser_async


def test_music_cache_key_prefers_media_id():
    """携带数据源原生 ID 的元数据应以 ID 作为缓存键主身份。"""
    cache = _build_music_cache({})
    meta = MetaMusic(
        title="晴天",
        artists=["周杰伦"],
        media_source="musicbrainz",
        media_id="rec-1",
    )

    cache.update(meta, _music_info())

    assert next(iter(cache._cache.data)).startswith("[音乐:v2]")
    renamed = MetaMusic(title="不同展示名", artists=["不同署名"], media_source="musicbrainz", media_id="rec-1")
    assert cache.get(renamed).media_id == "rec-1"


def test_music_cache_rebuilds_legacy_identity_keys(monkeypatch):
    """旧缓存未区分版本和实体范围，升级后不恢复其中可能串用的身份。"""
    file_cache = _FileCacheStub(pickle.dumps({
        "version": 1, "items": {"[音乐]legacy": {"expires_at": 2000, "value": _music_info().to_dict()}},
    }))
    runtime_cache = _TTLCacheStub()
    _build_initialized_music_cache(monkeypatch, file_cache, runtime_cache)
    assert runtime_cache.data == {}


def test_music_cache_update_and_get_roundtrip():
    """识别结果入缓存后应能还原出标准音乐信息，且不保存上游原始响应。"""
    cache = _build_music_cache({})
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美", year=2003)
    info = _music_info(raw_data={"payload": "large"})

    cache.update(meta, info)
    hit = cache.get(meta)

    assert hit is not None
    assert hit.media_id == "rec-1"
    assert hit.title == "晴天"
    assert hit.artists == ["周杰伦"]
    stored = next(iter(cache._cache.data.values()))
    assert "raw_data" not in stored


@pytest.mark.parametrize("field,value", [("version", "Live"), ("isrc", "USABC2600001")])
def test_music_cache_separates_recording_identity_evidence(field, value):
    """相同标题署名但版本或 ISRC 不同的请求不能共用识别结果。"""
    cache = _build_music_cache({})
    original = MetaMusic(title="晴天", artists=["周杰伦"])
    variant = MetaMusic.from_dict(original.to_dict())
    setattr(variant, field, value)
    cache.update(original, _music_info())
    assert cache.get(variant) is None


def test_music_cache_separates_requested_entity_scope():
    """单曲、专辑与未限定实体的请求分别缓存，负缓存也不能跨实体阻止回退。"""
    cache = _build_music_cache({})
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    cache.update(meta, _music_info(music_type="album"), music_type="album")
    cache.update(meta, MusicInfo.from_meta(meta), music_type="recording")
    assert cache.get(meta, music_type="album").music_type == "album"
    assert cache.get(meta, music_type="recording").media_id is None
    assert cache.get(meta) is None


def test_music_cache_key_does_not_confuse_field_separators():
    """名称中的连字符与多艺人分隔符不能把不同字段组合编码成同一个缓存键。"""
    cache = _build_music_cache({})
    original = MetaMusic(title="A-B", artists=["C"])
    other = MetaMusic(title="A", artists=["B-C"])
    cache.update(original, _music_info())
    assert cache.get(other) is None


def test_music_cache_get_miss_returns_none():
    """未命中的缓存查询应返回 None 而不是抛错。"""
    cache = _build_music_cache({})
    meta = MetaMusic(title="未知曲目")

    assert cache.get(meta) is None


def test_music_cache_list_items_normalizes_and_sorts():
    """管理列表应输出稳定顺序和前端展示所需字段。"""
    cache = _build_music_cache({
        "z-key": {
            "media_id": "rec-2",
            "title": "Zulu",
            "artists": ["歌手B"],
            "album": "专辑B",
            "year": 2024,
            "music_type": "recording",
        },
        "a-key": {
            "media_id": "",
            "title": "Alpha",
            "year": 2023,
        },
    })

    items = cache.list_items()

    assert [item["key"] for item in items] == ["a-key", "z-key"]
    assert items[0]["media_id"] == ""
    assert items[0]["artists"] == []
    assert items[0]["music_type"] == "recording"
    assert items[1]["artists"] == ["歌手B"]


def test_music_cache_delete_and_clear_persist_immediately(monkeypatch):
    """管理操作应修改运行时缓存并立即触发本地持久化。"""
    cache = _build_music_cache({"first": {"media_id": "rec-1"}, "second": {"media_id": "rec-2"}})
    saved_forces = []
    monkeypatch.setattr(cache, "save", lambda force=False: saved_forces.append(force))

    assert cache.delete("first") == {"media_id": "rec-1"}
    assert cache.delete("missing") == {}
    cache.clear()

    assert cache.list_items() == []
    assert saved_forces == [True, True]


def test_music_cache_restores_only_unexpired_persisted_items(monkeypatch):
    """持久化恢复应保留每条数据原有期限并跳过已过期条目。"""
    payload = {
        "version": music_cache_module.PERSISTENCE_VERSION,
        "items": {
            "fresh": {
                "value": {"media_id": "rec-1", "title": "有效"},
                "expires_at": 1030,
            },
            "expired": {
                "value": {"media_id": "rec-2", "title": "过期"},
                "expires_at": 999,
            },
        },
    }
    file_cache = _FileCacheStub(pickle.dumps(payload))
    runtime_cache = _TTLCacheStub()

    cache = _build_initialized_music_cache(
        monkeypatch=monkeypatch,
        file_cache=file_cache,
        runtime_cache=runtime_cache,
    )

    assert runtime_cache.data == {"fresh": {"media_id": "rec-1", "title": "有效"}}
    assert runtime_cache.ttls == {"fresh": 30}
    assert cache._expires_at == {"fresh": 1030}


def test_music_cache_persists_only_items_with_media_id(monkeypatch):
    """持久化应跳过未识别的负缓存条目，只保存携带远端身份的结果。"""
    file_cache = _FileCacheStub()
    runtime_cache = _TTLCacheStub()
    cache = _build_initialized_music_cache(
        monkeypatch=monkeypatch,
        file_cache=file_cache,
        runtime_cache=runtime_cache,
    )
    runtime_cache.data = {
        "recognized": {"media_id": "rec-1", "title": "晴天"},
        "negative": {"media_id": "", "title": "未知曲目"},
    }
    cache._expires_at = {
        "recognized": 1060,
        "negative": 1070,
    }
    cache._dirty = True

    cache.save()

    payload = pickle.loads(file_cache.content)
    assert file_cache.set_calls == [(
        music_cache_module.PERSISTENCE_KEY,
        music_cache_module.PERSISTENCE_REGION,
    )]
    assert payload == {
        "version": music_cache_module.PERSISTENCE_VERSION,
        "items": {
            "recognized": {
                "value": {"media_id": "rec-1", "title": "晴天"},
                "expires_at": 1060,
            },
        },
    }


def test_music_cache_save_removes_file_when_empty(monkeypatch):
    """全部条目失效后保存应删除持久化文件。"""
    file_cache = _FileCacheStub(pickle.dumps({"version": 1, "items": {}}))
    runtime_cache = _TTLCacheStub()
    cache = _build_initialized_music_cache(
        monkeypatch=monkeypatch,
        file_cache=file_cache,
        runtime_cache=runtime_cache,
    )
    cache._dirty = True

    cache.save()

    assert file_cache.delete_calls == [(
        music_cache_module.PERSISTENCE_KEY,
        music_cache_module.PERSISTENCE_REGION,
    )]


def test_music_cache_endpoint_returns_management_statistics(monkeypatch):
    """查询接口应返回识别成功和失败条目的统计。"""
    cache = _build_music_cache({
        "recognized": {"media_id": "rec-1", "title": "晴天"},
        "unrecognized": {"media_id": "", "title": "未知曲目"},
    })
    monkeypatch.setattr(
        music_endpoint.MusicBrainzChain, "cache_items", staticmethod(cache.list_items)
    )

    response = asyncio.run(music_endpoint.music_recognition_cache(None))

    assert response.success is True
    assert response.data["count"] == 2
    assert response.data["recognized"] == 1
    assert response.data["unrecognized"] == 1
    assert len(response.data["data"]) == 2


def test_music_cache_delete_endpoint_reports_missing_item(monkeypatch):
    """删除接口应区分成功删除与缓存不存在。"""
    cache = _build_music_cache({"existing": {"media_id": "rec-1"}})
    monkeypatch.setattr(
        music_endpoint.MusicBrainzChain, "delete_cache", staticmethod(cache.delete)
    )

    deleted_response = asyncio.run(
        music_endpoint.delete_music_recognition_cache("existing", None)
    )
    missing_response = asyncio.run(
        music_endpoint.delete_music_recognition_cache("missing", None)
    )

    assert deleted_response.success is True
    assert missing_response.success is False


def test_music_cache_clear_endpoint_removes_all_items(monkeypatch):
    """清空接口应删除全部音乐识别缓存。"""
    cache = _build_music_cache({"existing": {"media_id": "rec-1"}})
    monkeypatch.setattr(
        music_endpoint.MusicBrainzChain, "clear_cache", staticmethod(cache.clear)
    )

    response = asyncio.run(music_endpoint.clear_music_recognition_cache(None))

    assert response.success is True
    assert cache.list_items() == []


def _build_module_with_cache(cache: MusicBrainzCache) -> MusicBrainzModule:
    """构造挂载测试缓存的 MusicBrainz 模块实例。"""
    module = MusicBrainzModule()
    module.cache = cache
    return module


def test_module_recognize_media_hits_cache_without_search(monkeypatch):
    """识别缓存命中时应直接返回缓存结果，不再触发 MusicBrainz 搜索。"""
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美", year=2003)
    cache.update(meta, _music_info())
    search_mock = Mock()
    monkeypatch.setattr(module, "_search_recordings", search_mock)

    result = module.recognize_media(meta=meta)

    assert result is not None
    assert result.media_id == "rec-1"
    assert getattr(result, "recognize_cache_hit") is True
    search_mock.assert_not_called()


@pytest.mark.parametrize("async_mode", [False, True])
def test_module_recording_request_cannot_reuse_album_cache(monkeypatch, async_mode):
    """显式单曲识别不能读取此前未限定请求缓存下来的同名专辑。"""
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    cache.update(meta, _music_info(music_type="album", media_id="album-1"))
    expected = _music_info()
    monkeypatch.setattr(module, "_search_recordings", Mock(return_value=[expected]))
    monkeypatch.setattr(module, "_async_search_recordings", AsyncMock(return_value=[expected]))
    if async_mode:
        result = asyncio.run(module.async_recognize_media(meta=meta, music_type="recording"))
    else:
        result = module.recognize_media(meta=meta, music_type="recording")
    assert result is expected
    assert cache.get(meta, music_type="recording").music_type == "recording"
    if async_mode:
        cached = asyncio.run(module.async_recognize_media(meta=meta, music_type="recording"))
        module._async_search_recordings.assert_awaited_once()
    else:
        cached = module.recognize_media(meta=meta, music_type="recording")
        module._search_recordings.assert_called_once()
    assert cached.media_id == expected.media_id
    assert cached.recognize_cache_hit is True


def test_module_recognize_media_bypasses_cache_when_disabled(monkeypatch):
    """cache=False 时不读取缓存，重新走搜索识别流程。"""
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美", year=2003)
    cache.update(meta, _music_info())
    fresh = _music_info(media_id="rec-2")
    monkeypatch.setattr(module, "_search_recordings", Mock(return_value=[fresh]))
    monkeypatch.setattr(module, "_select_candidate", Mock(return_value=fresh))

    result = module.recognize_media(meta=meta, cache=False)

    assert result is fresh
    assert getattr(result, "recognize_cache_hit", False) is False


def test_module_recognize_media_writes_search_result_to_cache(monkeypatch):
    """搜索识别成功后应回填本地识别缓存。"""
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美", year=2003)
    matched = _music_info()
    monkeypatch.setattr(module, "_search_recordings", Mock(return_value=[matched]))
    monkeypatch.setattr(module, "_select_candidate", Mock(return_value=matched))

    result = module.recognize_media(meta=meta)

    assert result is matched
    hit = cache.get(meta)
    assert hit is not None
    assert hit.media_id == "rec-1"


def test_module_recognize_media_caches_offline_fallback(monkeypatch):
    """搜索无结果的兜底信息也应进入负缓存，避免重复请求。"""
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    meta = MetaMusic(title="未知曲目", artists=["未知艺术家"])
    monkeypatch.setattr(module, "_search_recordings", Mock(return_value=[]))
    monkeypatch.setattr(module, "_search_albums", Mock(return_value=[]))

    result = module.recognize_media(meta=meta)

    assert result is not None
    assert result.media_id is None
    cached = cache.get(meta)
    assert cached is not None
    assert cached.media_id is None
    assert cached.title == "未知曲目"


def test_module_update_recognize_cache_only_for_musicbrainz_music():
    """共享识别回填仅处理本数据源的音乐结果。"""
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    meta = MetaMusic(title="晴天", artists=["周杰伦"])

    assert module.update_recognize_cache(meta=meta, mediainfo=_music_info()) is True
    assert cache.get(meta) is not None

    other_source = _music_info(media_source="theaudiodb", media_id="x-1")
    assert module.update_recognize_cache(meta=meta, mediainfo=other_source) is None
    assert module.update_recognize_cache(meta=None, mediainfo=_music_info()) is None


@pytest.mark.parametrize("async_mode", [False, True])
def test_shared_recognition_replaces_entity_scoped_negative_cache(async_mode):
    """共享回填的公开契约没有请求类型，也必须覆盖已确认单曲对应的旧负缓存。"""
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    cache.update(meta, MusicInfo.from_meta(meta), music_type="recording")
    if async_mode:
        result = asyncio.run(module.async_update_recognize_cache(meta, _music_info()))
    else:
        result = module.update_recognize_cache(meta, _music_info())
    assert result is True
    assert cache.get(meta, music_type="recording").media_id == "rec-1"
    assert cache.get(meta).media_id == "rec-1"


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("isrc_identity", [False, True])
def test_cached_recording_rechecks_explicit_version_conflicts(monkeypatch, async_mode, isrc_identity):
    """旧正缓存不能绕过版本冲突确认；已核验的同一 ISRC 则保留身份优先级。"""
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    isrc = "USABC2600001" if isrc_identity else None
    meta = MetaMusic(title="Example Work", artists=["Artist"], version="Live 2001-05-02", isrc=isrc)
    cached = _music_info(title="Example Work", artists=["Artist"], version="Live 2001-05-03", isrc=isrc)
    fresh = _music_info(title="Example Work", artists=["Artist"], version=meta.version, media_id="fresh")
    cache.update(meta, cached, music_type="recording")
    monkeypatch.setattr(module, "_search_recordings", Mock(return_value=[fresh]))
    monkeypatch.setattr(module, "_async_search_recordings", AsyncMock(return_value=[fresh]))
    if async_mode:
        result = asyncio.run(module.async_recognize_media(meta=meta, music_type="recording"))
        assert module._async_search_recordings.await_count == (0 if isrc_identity else 1)
    else:
        result = module.recognize_media(meta=meta, music_type="recording")
        assert module._search_recordings.call_count == (0 if isrc_identity else 1)
    assert result.media_id == (cached.media_id if isrc_identity else fresh.media_id)
