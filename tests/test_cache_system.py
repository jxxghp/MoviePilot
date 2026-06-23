import asyncio
import os
import time

from app.core.cache import AsyncFileBackend, FileBackend, MemoryBackend
from app.core.config import settings
from app.helper.redis import AsyncRedisHelper, RedisHelper


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
