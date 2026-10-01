"""大体积二进制缓存的后端路由测试。

启用 Redis 缓存后端时，图片、种子、远程目录快照等二进制载荷必须留在本地
文件系统或进程内有界内存，小体积结构化缓存仍走 Redis。所有 Redis 访问均由
内存替身承接，不产生真实网络连接。
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

import pytest

import app.adapters.cache.redis as redis_adapter_module
import app.application.image as image_module
import app.monitor.snapshot as snapshot_module
from app.adapters.cache.backends import AsyncFileBackend, FileBackend
from app.adapters.cache.redis import AsyncRedisBackend, RedisBackend
from app.foundation.singleton import Singleton
from app.monitor.snapshot import SnapshotStore
from app.runtime.cache import (
    AsyncCache,
    AsyncFileCache,
    AsyncMemoryBackend,
    Cache,
    FileCache,
    MemoryBackend,
    cached,
)
from app.runtime.config import settings


class _FakeRedisHelper:
    """以进程内字典模拟 Redis 键空间，记录全部写入以便断言。"""

    store: Dict[Tuple[str, str], Any] = {}

    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            region: str = "DEFAULT", **_kwargs) -> None:
        """记录写入的键值。"""
        self.store[(region, key)] = value

    def get(self, key: str, region: str = "DEFAULT") -> Any:
        """读取已记录的键值。"""
        return self.store.get((region, key))

    def exists(self, key: str, region: str = "DEFAULT") -> bool:
        """判断键是否存在。"""
        return (region, key) in self.store

    def delete(self, key: str, region: str = "DEFAULT") -> None:
        """删除已记录的键。"""
        self.store.pop((region, key), None)


class _FakeAsyncRedisHelper:
    """异步 Redis 客户端替身，与同步替身共享键空间。"""

    async def set(self, key: str, value: Any, ttl: Optional[int] = None,
                  region: str = "DEFAULT", **_kwargs) -> None:
        """记录写入的键值。"""
        _FakeRedisHelper.store[(region, key)] = value

    async def get(self, key: str, region: str = "DEFAULT") -> Any:
        """读取已记录的键值。"""
        return _FakeRedisHelper.store.get((region, key))


@pytest.fixture
def redis_mode(monkeypatch, compose_cache_backend):
    """切换到 Redis 缓存后端，并用内存替身替换 Redis 客户端。"""
    _FakeRedisHelper.store = {}
    monkeypatch.setattr(redis_adapter_module, "RedisHelper", _FakeRedisHelper)
    monkeypatch.setattr(redis_adapter_module, "AsyncRedisHelper", _FakeAsyncRedisHelper)
    # 缓存类型在启动时选定，按启动流程重新装配后 Redis 路由才生效。
    compose_cache_backend("redis")
    return _FakeRedisHelper.store


def test_file_cache_default_keeps_redis_routing(redis_mode):
    """未声明 local_only 的调用方（含插件 SDK）在 Redis 模式下保持原有 Redis 路由。"""
    assert isinstance(FileCache(), RedisBackend)
    assert isinstance(AsyncFileCache(), AsyncRedisBackend)
    assert isinstance(Cache(), RedisBackend)
    assert isinstance(AsyncCache(), AsyncRedisBackend)


def test_local_only_file_cache_uses_filesystem_in_redis_mode(redis_mode, tmp_path):
    """local_only 的文件缓存在 Redis 模式下仍写入本地文件，不产生 Redis 键。"""
    cache = FileCache(base=tmp_path, ttl=60, local_only=True)
    async_cache = AsyncFileCache(base=tmp_path, ttl=60, local_only=True)

    cache.set("poster.jpg", b"\xff\xd8image", region="images")

    assert isinstance(cache, FileBackend)
    assert isinstance(async_cache, AsyncFileBackend)
    assert (tmp_path / "images" / "poster.jpg").read_bytes() == b"\xff\xd8image"
    assert asyncio.run(async_cache.get("poster.jpg", region="images")) == b"\xff\xd8image"
    assert redis_mode == {}


def test_local_only_memory_cache_ignores_redis_mode(redis_mode):
    """local_only 的通用缓存在 Redis 模式下使用进程内有界内存缓存。"""
    assert isinstance(Cache(maxsize=2, ttl=60, local_only=True), MemoryBackend)
    assert isinstance(AsyncCache(maxsize=2, ttl=60, local_only=True), AsyncMemoryBackend)


def test_file_cache_uses_filesystem_in_memory_mode(monkeypatch, tmp_path):
    """未启用 Redis 时文件缓存不受 local_only 影响，始终使用文件系统。"""
    monkeypatch.setattr(settings, "CACHE_BACKEND_TYPE", "cachetools")

    assert isinstance(FileCache(base=tmp_path), FileBackend)
    assert isinstance(FileCache(base=tmp_path, local_only=True), FileBackend)
    assert isinstance(AsyncFileCache(base=tmp_path), AsyncFileBackend)


def test_cached_local_only_keeps_values_out_of_redis(redis_mode):
    """cached(local_only=True) 在 Redis 模式下只缓存到进程内存。"""
    calls = []

    @cached(region="binary_routing_test", maxsize=4, ttl=60, local_only=True)
    def load(url: str) -> bytes:
        """返回固定二进制内容并记录调用次数。"""
        calls.append(url)
        return b"cover"

    assert load("https://example.test/a.jpg") == b"cover"
    assert load("https://example.test/a.jpg") == b"cover"
    assert calls == ["https://example.test/a.jpg"]
    assert redis_mode == {}


def test_image_helper_uses_filesystem_in_redis_mode(redis_mode, monkeypatch, tmp_path):
    """图片缓存在 Redis 模式下仍落在 CACHE_PATH/images。"""
    monkeypatch.delitem(
        Singleton._instances, (image_module.ImageHelper, (), frozenset()), raising=False
    )
    monkeypatch.setattr(
        image_module,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(cache_path=tmp_path, global_image_cache_days=7),
    )

    helper = image_module.ImageHelper()
    helper.file_cache.set("poster.jpg", b"\xff\xd8image", region="images")

    assert isinstance(helper.file_cache, FileBackend)
    assert isinstance(helper.async_file_cache, AsyncFileBackend)
    assert (tmp_path / "images" / "poster.jpg").exists()
    assert redis_mode == {}


def _snapshot_bytes(timestamp: float) -> bytes:
    """构造指定游标的快照内容。"""
    return json.dumps({
        "version": SnapshotStore.VERSION,
        "timestamp": timestamp,
        "file_count": 1,
        "snapshot": {"/mon/a.mkv": {"size": 1, "modify_time": timestamp}},
    }).encode("utf-8")


def test_snapshot_store_migrates_legacy_redis_snapshot(redis_mode, monkeypatch, tmp_path):
    """Redis 模式下升级前的快照可被读取，保存后落盘并删除 Redis 旧键。"""
    monkeypatch.setattr(snapshot_module, "get_runtime_setting", lambda _key: tmp_path)
    redis_mode[("snapshots", "alist_snapshot")] = _snapshot_bytes(100)
    store = SnapshotStore()

    data, ok = store.load_checked("alist")

    assert ok is True
    assert data["timestamp"] == 100

    assert store.save("alist", data["snapshot"], file_count=1, last_snapshot_time=100)

    snapshot_file = tmp_path / "snapshots" / "snapshots" / "alist_snapshot"
    assert json.loads(snapshot_file.read_bytes())["timestamp"] == 100
    assert ("snapshots", "alist_snapshot") not in redis_mode
    assert store.load_checked("alist")[0]["timestamp"] == 100


def test_snapshot_store_reset_clears_legacy_redis_snapshot(redis_mode, monkeypatch, tmp_path):
    """重置快照时同时清理 Redis 旧键，避免下次读取回退到旧基线。"""
    monkeypatch.setattr(snapshot_module, "get_runtime_setting", lambda _key: tmp_path)
    redis_mode[("snapshots", "alist_snapshot")] = _snapshot_bytes(100)
    store = SnapshotStore()

    assert store.reset("alist") is True
    assert ("snapshots", "alist_snapshot") not in redis_mode
    assert store.load_checked("alist") == (None, True)


def test_snapshot_store_has_no_legacy_source_in_memory_mode(monkeypatch, tmp_path):
    """未启用 Redis 时快照只读写本地文件，不存在旧数据来源。"""
    monkeypatch.setattr(settings, "CACHE_BACKEND_TYPE", "cachetools")
    monkeypatch.setattr(snapshot_module, "get_runtime_setting", lambda _key: tmp_path)
    store = SnapshotStore()

    assert store.save("alist", {"/mon/a.mkv": {"size": 1, "modify_time": 5}}, file_count=1)
    assert store.load_checked("alist")[0]["timestamp"] == 5
    assert (tmp_path / "snapshots" / "snapshots" / "alist_snapshot").exists()
