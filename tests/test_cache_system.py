import asyncio
import os
import threading
import time

from app.core.cache import (
    AsyncFileBackend,
    AsyncMemoryBackend,
    AsyncRedisBackend,
    AsyncFileCache,
    FileBackend,
    FileCache,
    MemoryBackend,
    RedisBackend,
    cached,
)
from app.core.config import settings
from app.helper.redis import AsyncRedisHelper, RedisHelper, serialize

def test_file_backend_items_keep_relative_keys_and_bytes(tmp_path):
    """
    文件缓存遍历应返回可继续删除的相对 key，并保持二进制内容不变。
    """
    cache = FileBackend(base=tmp_path)
    cache.set("nested/poster.jpg", b"\xff\xd8image", region="images")

    items = list(cache.items(region="images"))

    assert items == [("nested/poster.jpg", b"\xff\xd8image")]
    assert cache.popitem(region="images") == ("nested/poster.jpg", b"\xff\xd8image")
    assert not cache.exists("nested/poster.jpg", region="images")

def test_clear_package_tool_cache_only_removes_pip_and_uv_old_files(tmp_path, monkeypatch):
    """
    包安装工具缓存清理只处理 pip/uv 子目录，不接管整个 .cache 或业务缓存。
    """
    from app.startup.modules_initializer import clear_package_tool_cache

    old_time = time.time() - 40 * 24 * 3600
    cache_root = tmp_path / ".cache"
    old_pip = cache_root / "pip" / "old.whl"
    old_uv = cache_root / "uv" / "old.archive"
    unknown = cache_root / "other" / "old.bin"
    business = tmp_path / "cache" / "images" / "old.jpg"
    for path in (old_pip, old_uv, unknown, business):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        os.utime(path, (old_time, old_time))

    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PACKAGE_CACHE_ROOT", None)
    monkeypatch.setattr(settings, "PACKAGE_CACHE_DAYS", 30)

    clear_package_tool_cache()

    assert not old_pip.exists()
    assert not old_uv.exists()
    assert unknown.exists()
    assert business.exists()

def test_clear_package_tool_cache_disabled_when_days_non_positive(tmp_path, monkeypatch):
    """
    PACKAGE_CACHE_DAYS 小于等于 0 时不清理包安装缓存。
    """
    from app.startup.modules_initializer import clear_package_tool_cache

    old_time = time.time() - 40 * 24 * 3600
    old_pip = tmp_path / ".cache" / "pip" / "old.whl"
    old_pip.parent.mkdir(parents=True, exist_ok=True)
    old_pip.write_text("x", encoding="utf-8")
    os.utime(old_pip, (old_time, old_time))

    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PACKAGE_CACHE_ROOT", None)
    monkeypatch.setattr(settings, "PACKAGE_CACHE_DAYS", 0)

    clear_package_tool_cache()

    assert old_pip.exists()

def test_clear_package_tool_cache_isolates_subdir_errors(tmp_path, monkeypatch):
    """
    单个工具缓存目录清理失败，不影响另一个工具缓存目录。
    """
    from app.startup.modules_initializer import clear_package_tool_cache

    calls = []

    def fake_clear(path, days):
        calls.append((path.name, days))
        if path.name == "pip":
            raise OSError("pip cache locked")

    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PACKAGE_CACHE_ROOT", str(tmp_path / "custom-package-cache"))
    monkeypatch.setattr(settings, "PACKAGE_CACHE_DAYS", 30)
    monkeypatch.setattr("app.startup.modules_initializer.SystemUtils.clear", fake_clear)

    clear_package_tool_cache()

    assert calls == [("pip", 30), ("uv", 30)]

def test_clear_package_tool_cache_uses_package_cache_root(tmp_path, monkeypatch):
    """
    PACKAGE_CACHE_ROOT 用作 pip/uv 清理根目录，不扩大到配置目录下其他缓存。
    """
    from app.startup.modules_initializer import clear_package_tool_cache

    old_time = time.time() - 40 * 24 * 3600
    package_cache_root = tmp_path / "custom-package-cache"
    old_pip = package_cache_root / "pip" / "old.whl"
    default_pip = tmp_path / ".cache" / "pip" / "old.whl"
    for path in (old_pip, default_pip):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        os.utime(path, (old_time, old_time))

    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PACKAGE_CACHE_ROOT", str(package_cache_root))
    monkeypatch.setattr(settings, "PACKAGE_CACHE_DAYS", 30)

    clear_package_tool_cache()

    assert not old_pip.exists()
    assert default_pip.exists()

def test_init_modules_does_not_clear_package_tool_cache(monkeypatch):
    """
    包安装缓存清理由通用临时清理入口触发，模块启动路径不直接执行清理。
    """
    from app.startup import modules_initializer

    called = False

    def fail_if_called():
        nonlocal called
        called = True
        raise AssertionError("init_modules must not clear package tool cache directly")

    monkeypatch.setattr(modules_initializer, "clear_package_tool_cache", fail_if_called)
    monkeypatch.setattr(modules_initializer, "DisplayHelper", lambda: None)
    monkeypatch.setattr(modules_initializer, "DohHelper", lambda: None)
    monkeypatch.setattr(modules_initializer, "SitesHelper", lambda: None)
    monkeypatch.setattr(modules_initializer, "ResourceHelper", lambda: None)
    monkeypatch.setattr(modules_initializer, "user_auth", lambda: None)
    monkeypatch.setattr(modules_initializer, "ModuleManager", lambda: None)
    monkeypatch.setattr(modules_initializer.EventManager, "start", lambda self: None)
    monkeypatch.setattr(modules_initializer.MoviePilotServerHelper, "init_plugin_report", lambda: None)
    monkeypatch.setattr(modules_initializer.MoviePilotServerHelper, "init_subscribe_report", lambda: None)
    monkeypatch.setattr(modules_initializer.MoviePilotServerHelper, "get_user_uuid", lambda: None)
    monkeypatch.setattr(modules_initializer.MoviePilotServerHelper, "get_github_user", lambda: None)
    monkeypatch.setattr(modules_initializer, "init_agent", lambda: None)
    monkeypatch.setattr(modules_initializer, "start_frontend", lambda: None)
    monkeypatch.setattr(modules_initializer, "check_auth", lambda: None)

    modules_initializer.init_modules()

    assert called is False

def test_file_backend_delete_missing_key_is_noop(tmp_path):
    """
    删除不存在的文件缓存 key 应保持幂等，不向调用方抛出文件系统异常。
    """
    cache = FileBackend(base=tmp_path)

    cache.delete("missing", region="default")

    assert not cache.exists("missing", region="default")

def test_memory_backend_delete_missing_key_is_noop():
    """
    内存缓存后端 delete 与其他后端保持一致，不存在时直接返回。
    """
    cache = MemoryBackend()

    cache.delete("missing", region="missing_delete")

    assert not cache.exists("missing", region="missing_delete")

def test_memory_backend_supports_per_key_ttl():
    """
    同一 region 的 key 应按各自 TTL 过期，不受首个 key 的 TTL 影响。
    """
    region = "per_key_ttl"
    cache = MemoryBackend()
    cache.set("short", "short-value", ttl=10, region=region)
    cache.set("long", "long-value", ttl=20, region=region)
    region_cache = MemoryBackend._region_caches[cache.get_region(region)]
    started_at = region_cache.timer()

    region_cache.expire(time=started_at + 11)

    assert cache.get("short", region=region) is None
    assert cache.get("long", region=region) == "long-value"
    assert list(cache.items(region=region)) == [("long", "long-value")]

def test_memory_backend_resets_ttl_when_key_is_rewritten():
    """
    重写已有 key 时应从重写时刻按新 TTL 重新计算过期时间。
    """
    region = "rewrite_per_key_ttl"
    cache = MemoryBackend()
    cache.set("key", "old", ttl=10, region=region)
    region_cache = MemoryBackend._region_caches[cache.get_region(region)]
    started_at = region_cache.timer()

    cache.set("key", "new", ttl=20, region=region)
    region_cache.expire(time=started_at + 11)

    assert cache.get("key", region=region) == "new"

    region_cache.expire(time=started_at + 21)

    assert cache.get("key", region=region) is None

def test_memory_backend_instances_share_region_with_per_key_ttl():
    """
    多个 backend 仍共享 region 数据，但每次写入的显式 TTL 应独立生效。
    """
    region = "shared_per_key_ttl"
    first = MemoryBackend()
    second = MemoryBackend()
    first.set("first", "first-value", ttl=10, region=region)
    second.set("second", "second-value", ttl=20, region=region)
    region_cache = MemoryBackend._region_caches[first.get_region(region)]
    started_at = region_cache.timer()

    region_cache.expire(time=started_at + 11)

    assert second.get("first", region=region) is None
    assert first.get("second", region=region) == "second-value"

def test_async_memory_backend_supports_per_key_ttl():
    """
    异步代理路径应与同步 backend 共享 region，并保留每个 key 的 TTL。
    """
    async def run_test():
        region = "async_per_key_ttl"
        sync_cache = MemoryBackend()
        async_cache = AsyncMemoryBackend()
        await async_cache.set("short", "short-value", ttl=10, region=region)
        sync_cache.set("long", "long-value", ttl=20, region=region)
        region_cache = MemoryBackend._region_caches[sync_cache.get_region(region)]
        started_at = region_cache.timer()
        region_cache.expire(time=started_at + 11)

        assert await async_cache.get("short", region=region) is None
        assert await async_cache.get("long", region=region) == "long-value"

    asyncio.run(run_test())

def test_memory_lru_backend_keeps_capacity_eviction_behavior():
    """
    per-key TTL 改造不应影响 LRU region 的容量淘汰行为。
    """
    region = "memory_lru"
    cache = MemoryBackend(cache_type="lru", maxsize=2)
    cache.set("first", 1, region=region)
    cache.set("second", 2, region=region)
    cache.set("third", 3, region=region)

    assert cache.get("first", region=region) is None
    assert list(cache.items(region=region)) == [("second", 2), ("third", 3)]

def test_cached_zero_ttl_does_not_cache_sync_result():
    """
    同步 cached(ttl=0) 应立即过期，不能退化为 LRU 永久缓存。
    """
    calls = 0

    @cached(region="sync_zero_ttl", ttl=0)
    def load_value():
        nonlocal calls
        calls += 1
        return calls

    assert load_value() == 1
    assert load_value() == 2

def test_cached_zero_ttl_does_not_cache_async_result():
    """
    异步 cached(ttl=0) 应与同步路径保持一致。
    """
    calls = 0

    @cached(region="async_zero_ttl", ttl=0)
    async def load_value():
        nonlocal calls
        calls += 1
        return calls

    async def run_test():
        return await load_value(), await load_value()

    assert asyncio.run(run_test()) == (1, 2)


def test_memory_backend_global_clear_is_safe_during_region_creation():
    """
    全局清理与新 region 创建应由同一把锁串行化，不能并发修改注册表。
    """
    cache = MemoryBackend()
    cache.set("existing", 1, region="clear_existing")
    started = threading.Event()
    release = threading.Event()
    region_cache = MemoryBackend._region_caches[cache.get_region("clear_existing")]
    original_clear = region_cache.clear

    def blocking_clear():
        started.set()
        release.wait(timeout=5)
        original_clear()

    region_cache.clear = blocking_clear
    clear_thread = threading.Thread(target=cache.clear, args=(None,))
    clear_thread.start()
    assert started.wait(timeout=5)

    set_thread = threading.Thread(
        target=cache.set,
        args=("new", 2),
        kwargs={"region": "clear_new"},
    )
    set_thread.start()
    set_thread.join(timeout=0.1)

    assert set_thread.is_alive()

    release.set()
    clear_thread.join(timeout=5)
    set_thread.join(timeout=5)

    assert not clear_thread.is_alive()
    assert not set_thread.is_alive()
    assert cache.get("existing", region="clear_existing") is None
    assert cache.get("new", region="clear_new") == 2


def test_memory_backend_rejects_region_cache_type_conflicts():
    """
    同名 region 不得同时作为 TTL 和 LRU 缓存使用。
    """
    region = "cache_type_conflict"
    ttl_cache = MemoryBackend(cache_type="ttl")
    lru_cache = MemoryBackend(cache_type="lru")
    ttl_cache.set("ttl", 1, ttl=10, region=region)

    try:
        lru_cache.set("lru", 2, region=region)
    except ValueError as err:
        assert "different cache type" in str(err)
    else:
        raise AssertionError("cache type conflict must be rejected")

    assert ttl_cache.get("ttl", region=region) == 1
    assert ttl_cache.get("lru", region=region) is None

def test_memory_backend_reuses_existing_region_cache():
    """
    同一 region 的后续写入应复用首次创建的底层缓存对象。
    """
    region = "reuse_region_cache"
    cache = MemoryBackend()
    cache.set("first", 1, ttl=10, region=region)
    first_region_cache = MemoryBackend._region_caches[cache.get_region(region)]

    cache.set("second", 2, ttl=20, region=region)

    assert MemoryBackend._region_caches[cache.get_region(region)] is first_region_cache


def test_memory_backend_uses_default_maxsize_for_zero_override():
    """
    动态 maxsize=0 应与构造参数一致，回退到 backend 默认容量。
    """
    region = "zero_maxsize_override"
    cache = MemoryBackend(maxsize=8)
    cache.set("key", "value", maxsize=0, region=region)

    region_cache = MemoryBackend._region_caches[cache.get_region(region)]

    assert region_cache.maxsize == 8
    assert cache.get("key", region=region) == "value"


def test_memory_backend_preserves_zero_ttl():
    """
    显式 ttl=0 不应回退到默认 TTL，并应删除已有同名值。
    """
    cache = MemoryBackend(ttl=30)
    cache.set("key", "old", region="zero_ttl")
    cache.set("key", "new", ttl=0, region="zero_ttl")

    assert cache.get("key", region="zero_ttl") is None

def test_memory_backend_preserves_negative_ttl():
    """
    显式负 TTL 应保持立即过期语义，并删除已有同名值。
    """
    cache = MemoryBackend(ttl=30)
    cache.set("key", "old", region="negative_ttl")
    cache.set("key", "new", ttl=-1, region="negative_ttl")

    assert cache.get("key", region="negative_ttl") is None

def test_memory_backend_uses_zero_default_ttl():
    """
    backend 的默认 ttl=0 应保持立即过期语义。
    """
    cache = MemoryBackend(ttl=0)
    cache.set("key", "value", region="zero_default_ttl")

    assert cache.get("key", region="zero_default_ttl") is None

def test_redis_backend_treats_zero_ttl_as_expired():
    """
    Redis backend 应删除 ttl=0 的同名 key，避免向 Redis 发送无效 EX 0。
    """
    class RedisHelperStub:
        deleted = None
        set_called = False

        def set(self, key, value, ttl, region, **kwargs):
            self.set_called = True

        def delete(self, key, region):
            self.deleted = (key, region)

    cache = object.__new__(RedisBackend)
    cache.ttl = 30
    cache.redis_helper = RedisHelperStub()

    cache.set("key", "value", ttl=0, region="zero_ttl")

    assert cache.redis_helper.deleted == ("key", "zero_ttl")
    assert not cache.redis_helper.set_called

def test_async_redis_backend_treats_zero_ttl_as_expired():
    """
    异步 Redis backend 应删除 ttl=0 的同名 key，不发送无效 EX 0。
    """
    class AsyncRedisHelperStub:
        deleted = None
        set_called = False

        async def set(self, key, value, ttl, region, **kwargs):
            self.set_called = True

        async def delete(self, key, region):
            self.deleted = (key, region)

    async def run_test():
        cache = object.__new__(AsyncRedisBackend)
        cache.ttl = 30
        cache.redis_helper = AsyncRedisHelperStub()
        await cache.set("key", "value", ttl=0, region="zero_ttl")
        return cache.redis_helper

    helper = asyncio.run(run_test())

    assert helper.deleted == ("key", "zero_ttl")
    assert not helper.set_called

def test_file_cache_preserves_zero_ttl_in_redis_mode(monkeypatch):
    """
    FileCache 在 Redis 模式下不应把显式 ttl=0 替换为临时文件默认 TTL。
    """
    monkeypatch.setattr(settings, "CACHE_BACKEND_TYPE", "redis")

    assert FileCache(ttl=0).ttl == 0


def test_async_file_cache_preserves_zero_ttl_in_redis_mode(monkeypatch):
    """
    AsyncFileCache 在 Redis 模式下应与同步工厂保持相同 TTL 语义。
    """
    monkeypatch.setattr(settings, "CACHE_BACKEND_TYPE", "redis")

    assert AsyncFileCache(ttl=0).ttl == 0


def test_file_cache_uses_default_ttl_when_omitted(monkeypatch):
    """
    未传 TTL 时仍使用 TEMP_FILE_DAYS 配置的默认值。
    """
    monkeypatch.setattr(settings, "CACHE_BACKEND_TYPE", "redis")
    monkeypatch.setattr(settings, "TEMP_FILE_DAYS", 7)

    assert FileCache().ttl == 7 * 24 * 3600
    assert AsyncFileCache().ttl == 7 * 24 * 3600


def test_redis_original_key_decodes_quoted_key():
    """
    Redis items 返回的 key 应还原为原始缓存 key，确保带特殊字符的 key 可继续删除。
    """
    redis_key = b"region:DEFAULT:key:nested/poster%20one.jpg"

    assert RedisHelper._RedisHelper__get_original_key(redis_key) == "nested/poster one.jpg"

def test_redis_helper_uses_blocking_pool_settings(monkeypatch):
    """
    Redis 同步客户端应使用阻塞连接池，避免并发峰值直接耗尽 Redis 连接数。
    """
    calls = {}

    class FakeClient:
        """模拟同步 Redis 客户端。"""

        def __init__(self, connection_pool):
            self.connection_pool = connection_pool
            self.config_calls = []
            self.closed = False

        def ping(self):
            """模拟 Redis ping。"""
            calls["ping"] = True

        def config_set(self, key, value):
            """记录 Redis 配置写入。"""
            self.config_calls.append((key, value))

        def close(self):
            """标记客户端已关闭。"""
            self.closed = True

    def fake_from_url(url, **kwargs):
        """记录连接池构造参数。"""
        calls["pool"] = {"url": url, **kwargs}
        return "pool"

    monkeypatch.setattr(settings, "CACHE_BACKEND_URL", "redis://cache:6379/2")
    monkeypatch.setattr(settings, "CACHE_REDIS_MAX_CONNECTIONS", 7)
    monkeypatch.setattr(settings, "CACHE_REDIS_POOL_TIMEOUT", 3)
    monkeypatch.setattr("app.helper.redis.redis.BlockingConnectionPool.from_url", fake_from_url)
    monkeypatch.setattr("app.helper.redis.redis.Redis", FakeClient)

    helper = RedisHelper()
    helper.close()
    helper._connect()

    assert calls["pool"]["url"] == "redis://cache:6379/2"
    assert calls["pool"]["max_connections"] == 7
    assert calls["pool"]["timeout"] == 3
    assert calls["pool"]["decode_responses"] is False
    assert calls["ping"] is True
    assert ("maxmemory-policy", "allkeys-lru") in helper.client.config_calls

    helper.close()


def test_redis_helper_pop_uses_atomic_getdel():
    """Redis 缓存领取必须通过单条 GETDEL 命令完成。"""
    calls = []

    class FakeClient:
        def getdel(self, key):
            calls.append(key)
            return serialize({"challenge": "value"})

    helper = RedisHelper()
    helper.client = FakeClient()
    try:
        value = helper.pop("token", region="passkey_challenge")
    finally:
        helper.client = None

    assert value == {"challenge": "value"}
    assert calls == ["region:passkey_challenge:key:token"]


def test_async_redis_helper_uses_blocking_pool_settings(monkeypatch):
    """
    Redis 异步客户端应使用阻塞连接池，避免高并发缓存读取立刻抛出连接耗尽错误。
    """
    calls = {}

    class FakeAsyncClient:
        """模拟异步 Redis 客户端。"""

        def __init__(self, connection_pool):
            self.connection_pool = connection_pool
            self.config_calls = []
            self.closed = False

        async def ping(self):
            """模拟 Redis ping。"""
            calls["ping"] = True

        async def config_set(self, key, value):
            """记录 Redis 配置写入。"""
            self.config_calls.append((key, value))

        async def close(self):
            """标记客户端已关闭。"""
            self.closed = True

    def fake_from_url(url, **kwargs):
        """记录连接池构造参数。"""
        calls["pool"] = {"url": url, **kwargs}
        return "async_pool"

    async def run_connect():
        helper = AsyncRedisHelper()
        await helper.close()
        await helper._connect()
        config_calls = list(helper.client.config_calls)
        await helper.close()
        return config_calls

    monkeypatch.setattr(settings, "CACHE_BACKEND_URL", "redis://cache:6379/3")
    monkeypatch.setattr(settings, "CACHE_REDIS_MAX_CONNECTIONS", 9)
    monkeypatch.setattr(settings, "CACHE_REDIS_POOL_TIMEOUT", 4)
    monkeypatch.setattr("app.helper.redis.AsyncBlockingConnectionPool.from_url", fake_from_url)
    monkeypatch.setattr("app.helper.redis.Redis", FakeAsyncClient)

    config_calls = asyncio.run(run_connect())

    assert calls["pool"]["url"] == "redis://cache:6379/3"
    assert calls["pool"]["max_connections"] == 9
    assert calls["pool"]["timeout"] == 4
    assert calls["pool"]["decode_responses"] is False
    assert calls["ping"] is True
    assert ("maxmemory-policy", "allkeys-lru") in config_calls

def test_redis_helpers_watch_pool_settings():
    """
    Redis 连接池配置变化应触发客户端重建。
    """
    assert "CACHE_REDIS_MAX_CONNECTIONS" in RedisHelper.CONFIG_WATCH
    assert "CACHE_REDIS_POOL_TIMEOUT" in RedisHelper.CONFIG_WATCH
    assert "CACHE_REDIS_MAX_CONNECTIONS" in AsyncRedisHelper.CONFIG_WATCH
    assert "CACHE_REDIS_POOL_TIMEOUT" in AsyncRedisHelper.CONFIG_WATCH

def test_async_file_backend_missing_region_has_no_items(tmp_path):
    """
    异步文件缓存缺失区域时应返回空迭代，而不是伪造空 key。
    """

    async def collect_items():
        cache = AsyncFileBackend(base=tmp_path)
        return [item async for item in cache.items(region="missing")]

    assert asyncio.run(collect_items()) == []

def test_async_file_backend_items_keep_relative_keys_and_bytes(tmp_path):
    """
    异步文件缓存遍历应与同步文件缓存保持相同 key 和二进制语义。
    """

    async def collect_items():
        cache = AsyncFileBackend(base=tmp_path)
        await cache.set("nested/poster.jpg", b"\xff\xd8image", region="images")
        items = [item async for item in cache.items(region="images")]
        popped = await cache.popitem(region="images")
        exists = await cache.exists("nested/poster.jpg", region="images")
        return items, popped, exists

    items, popped, exists = asyncio.run(collect_items())

    assert items == [("nested/poster.jpg", b"\xff\xd8image")]
    assert popped == ("nested/poster.jpg", b"\xff\xd8image")
    assert not exists

def test_file_backend_items_skip_directories(tmp_path):
    """
    文件缓存遍历应递归读取有效缓存文件，不把目录当成缓存项。
    """
    cache = FileBackend(base=tmp_path)
    cache.set("nested/value", b"value", region="region")
    (tmp_path / "region" / "empty_dir").mkdir()

    assert list(cache.items(region="region")) == [("nested/value", b"value")]
