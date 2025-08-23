#!/usr/bin/env python3
"""
缓存工具模块

提供更好的异步缓存使用方式，解决AsyncCacheBackend dict-like方法不支持async的问题
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, AsyncGenerator, Tuple, Union
from functools import wraps

from app.core.cache import AsyncCacheBackend


class AsyncCacheContext:
    """
    异步缓存上下文管理器
    
    提供更优雅的异步缓存使用方式
    """
    
    def __init__(self, cache: AsyncCacheBackend):
        self.cache = cache
        self._loop = None
    
    def __enter__(self):
        """同步上下文入口"""
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """同步上下文出口"""
        pass
    
    async def __aenter__(self):
        """异步上下文入口"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文出口"""
        await self.cache.close()
    
    def __getitem__(self, key: str) -> Any:
        """同步获取"""
        return self._loop.run_until_complete(self.cache.get(key))
    
    def __setitem__(self, key: str, value: Any) -> None:
        """同步设置"""
        self._loop.run_until_complete(self.cache.set(key, value))
    
    def __delitem__(self, key: str) -> None:
        """同步删除"""
        exists = self._loop.run_until_complete(self.cache.exists(key))
        if not exists:
            raise KeyError(key)
        self._loop.run_until_complete(self.cache.delete(key))
    
    def __contains__(self, key: str) -> bool:
        """同步检查存在"""
        return self._loop.run_until_complete(self.cache.exists(key))
    
    def __iter__(self):
        """同步迭代"""
        items = self._loop.run_until_complete(self._get_all_items())
        for key, _ in items:
            yield key
    
    def __len__(self) -> int:
        """同步长度"""
        items = self._loop.run_until_complete(self._get_all_items())
        return len(items)
    
    async def _get_all_items(self) -> list:
        """获取所有项目"""
        items = []
        async for item in self.cache.items():
            items.append(item)
        return items


class AsyncCacheDict:
    """
    异步缓存字典包装器
    
    提供类似dict的接口，但支持异步操作
    """
    
    def __init__(self, cache: AsyncCacheBackend):
        self.cache = cache
    
    async def __getitem__(self, key: str) -> Any:
        """异步获取"""
        value = await self.cache.get(key)
        if value is None:
            raise KeyError(key)
        return value
    
    async def __setitem__(self, key: str, value: Any) -> None:
        """异步设置"""
        await self.cache.set(key, value)
    
    async def __delitem__(self, key: str) -> None:
        """异步删除"""
        if not await self.cache.exists(key):
            raise KeyError(key)
        await self.cache.delete(key)
    
    async def __contains__(self, key: str) -> bool:
        """异步检查存在"""
        return await self.cache.exists(key)
    
    async def __aiter__(self):
        """异步迭代"""
        async for key, _ in self.cache.items():
            yield key
    
    async def __len__(self) -> int:
        """异步长度"""
        count = 0
        async for _ in self.cache.items():
            count += 1
        return count
    
    async def get(self, key: str, default: Any = None) -> Any:
        """异步获取，支持默认值"""
        value = await self.cache.get(key)
        return value if value is not None else default
    
    async def setdefault(self, key: str, default: Any = None) -> Any:
        """异步设置默认值"""
        value = await self.cache.get(key)
        if value is None:
            await self.cache.set(key, default)
            return default
        return value
    
    async def update(self, other: Dict[str, Any]) -> None:
        """异步更新"""
        for key, value in other.items():
            await self.cache.set(key, value)
    
    async def pop(self, key: str, default: Any = None) -> Any:
        """异步弹出"""
        value = await self.cache.get(key)
        if value is not None:
            await self.cache.delete(key)
            return value
        if default is not None:
            return default
        raise KeyError(key)
    
    async def clear(self) -> None:
        """异步清空"""
        await self.cache.clear()
    
    async def keys(self) -> AsyncGenerator[str, None]:
        """异步获取所有键"""
        async for key, _ in self.cache.items():
            yield key
    
    async def values(self) -> AsyncGenerator[Any, None]:
        """异步获取所有值"""
        async for _, value in self.cache.items():
            yield value
    
    async def items(self) -> AsyncGenerator[Tuple[str, Any], None]:
        """异步获取所有项目"""
        async for item in self.cache.items():
            yield item


def async_cache_context(cache: AsyncCacheBackend):
    """
    装饰器：为异步缓存提供上下文管理
    
    Usage:
        @async_cache_context(cache)
        async def my_function(cache_ctx):
            cache_ctx["key"] = "value"
            value = cache_ctx["key"]
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with AsyncCacheContext(cache) as ctx:
                return await func(ctx, *args, **kwargs)
        return wrapper
    return decorator


def sync_cache_context(cache: AsyncCacheBackend):
    """
    装饰器：为同步函数提供缓存上下文管理
    
    Usage:
        @sync_cache_context(cache)
        def my_function(cache_ctx):
            cache_ctx["key"] = "value"
            value = cache_ctx["key"]
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with AsyncCacheContext(cache) as ctx:
                return func(ctx, *args, **kwargs)
        return wrapper
    return decorator


# 便捷函数
def create_async_dict(cache: AsyncCacheBackend) -> AsyncCacheDict:
    """创建异步字典包装器"""
    return AsyncCacheDict(cache)


def create_cache_context(cache: AsyncCacheBackend) -> AsyncCacheContext:
    """创建缓存上下文管理器"""
    return AsyncCacheContext(cache)


# 使用示例
async def example_usage():
    """使用示例"""
    from app.core.cache import AsyncFileCache
    
    # 创建缓存实例
    cache = AsyncFileCache()
    
    # 方式1：使用异步字典包装器
    async_dict = create_async_dict(cache)
    
    await async_dict["key1"] = "value1"
    value = await async_dict["key1"]
    print(f"Value: {value}")
    
    # 方式2：使用上下文管理器
    async with create_cache_context(cache) as ctx:
        ctx["key2"] = "value2"
        value = ctx["key2"]
        print(f"Value: {value}")
    
    # 方式3：使用装饰器
    @async_cache_context(cache)
    async def my_async_function(cache_ctx):
        cache_ctx["key3"] = "value3"
        return cache_ctx["key3"]
    
    result = await my_async_function()
    print(f"Result: {result}")
    
    await cache.close()


if __name__ == "__main__":
    asyncio.run(example_usage())