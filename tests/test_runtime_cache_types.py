"""
缓存类型收敛期间的区域转发、异步元数据与上下文恢复契约。
"""

import asyncio

import pytest

from app.runtime.cache import (
    AsyncCacheProxy,
    AsyncMemoryBackend,
    CacheProxy,
    MemoryBackend,
    async_fresh,
    cached,
    fresh,
    is_fresh,
)


@pytest.fixture(autouse=True)
def isolated_regions(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    每个用例使用独立区域表，结束后恢复进程原有缓存。
    """
    monkeypatch.setattr(MemoryBackend, "_region_caches", {})


def test_proxy_region_override_and_zero_ttl() -> None:
    """
    显式区域覆盖与零 TTL 保持原有转发语义，映射操作仍使用绑定区域。
    """
    backend = MemoryBackend(ttl=60)
    proxy = CacheProxy(backend, None)
    proxy["bound"] = b"binary"
    proxy.set("other", 2, region="alternate", maxsize=2, ttl=60)
    assert list(proxy.items()) == [("bound", b"binary")]
    assert list(proxy.keys(region="alternate")) == ["other"]
    assert list(proxy.values(region="alternate")) == [2]
    proxy.set("other", 3, region="alternate", ttl=0)
    assert not proxy.exists("other", region="alternate")
    assert proxy.pop("bound") == b"binary"
    assert len(proxy) == 0


def test_async_proxy_and_decorator_management() -> None:
    """
    异步代理迭代与缓存管理回调仍可等待，删除操作只影响指定参数键。
    """
    calls = []

    @cached(region="typed-wrapper", local_only=True)
    async def fetch(key: str) -> str:
        """
        记录未命中缓存时发生的实际调用。
        """
        calls.append(key)
        return key.upper()

    async def exercise() -> None:
        """
        在单一事件循环中验证代理和装饰器的异步契约。
        """
        proxy = AsyncCacheProxy(AsyncMemoryBackend(), None)
        await proxy.set("item", b"data", region="alternate", maxsize=2)
        assert [item async for item in proxy.items(region="alternate")] == [("item", b"data")]
        assert [key async for key in proxy.keys(region="alternate")] == ["item"]
        assert [value async for value in proxy.values(region="alternate")] == [b"data"]
        assert await fetch("a") == "A"
        assert await fetch("a") == "A"
        assert calls == ["a"]
        assert await fetch.cache_exists("a")
        await fetch.cache_delete("a")
        assert not await fetch.cache_exists("a")
        await fetch("b")
        await fetch.cache_clear()
        assert not await fetch.cache_exists("b")

    asyncio.run(exercise())


def test_fresh_context_restores_after_exception() -> None:
    """
    同步和异步嵌套刷新上下文在异常退出后都恢复外层状态。
    """
    original = is_fresh()
    with pytest.raises(ValueError), fresh():
        with fresh(False):
            assert is_fresh()
            raise ValueError("测试退出")
    assert is_fresh() is original

    async def exercise() -> None:
        """
        异步上下文在异常和嵌套退出时恢复令牌。
        """
        with pytest.raises(ValueError):
            async with async_fresh():
                async with async_fresh(False):
                    assert is_fresh()
                    raise ValueError("测试退出")
        assert is_fresh() is original

    asyncio.run(exercise())
