#!/usr/bin/env python3
"""
AsyncCacheBackend dict-like方法使用示例

这个示例展示了如何处理AsyncCacheBackend的dict-like方法不支持async的问题
"""

import asyncio
from app.core.cache import AsyncFileCache


async def async_usage_example():
    """
    异步使用示例 - 推荐方式
    """
    print("=== 异步使用示例 ===")
    
    # 创建异步缓存实例
    cache = AsyncFileCache()
    
    # 使用异步方法
    await cache.set("key1", "value1")
    await cache.set("key2", "value2")
    
    # 获取值
    value1 = await cache.get("key1")
    print(f"async get: {value1}")
    
    # 检查键是否存在
    exists = await cache.exists("key1")
    print(f"async exists: {exists}")
    
    # 使用异步的dict-like方法
    value1_async = await cache.async_getitem("key1")
    print(f"async __getitem__: {value1_async}")
    
    await cache.async_setitem("key3", "value3")
    print("async __setitem__: set key3")
    
    # 异步迭代
    print("async iteration:")
    async for key, value in cache.items():
        print(f"  {key}: {value}")
    
    # 异步长度
    length = await cache.async_len()
    print(f"async length: {length}")
    
    await cache.close()


def sync_usage_example():
    """
    同步使用示例 - 使用同步包装器
    """
    print("\n=== 同步使用示例 ===")
    
    # 创建异步缓存实例
    cache = AsyncFileCache()
    
    # 使用同步的dict-like方法（内部会调用异步方法）
    cache["sync_key1"] = "sync_value1"
    cache["sync_key2"] = "sync_value2"
    
    # 获取值
    value1 = cache["sync_key1"]
    print(f"sync __getitem__: {value1}")
    
    # 检查键是否存在
    exists = "sync_key1" in cache
    print(f"sync __contains__: {exists}")
    
    # 同步迭代
    print("sync iteration:")
    for key in cache:
        print(f"  {key}")
    
    # 同步长度
    length = len(cache)
    print(f"sync length: {length}")
    
    # 删除键
    del cache["sync_key1"]
    print("deleted sync_key1")
    
    # 检查删除后的长度
    new_length = len(cache)
    print(f"new length: {new_length}")
    
    # 关闭缓存
    asyncio.run(cache.close())


def mixed_usage_example():
    """
    混合使用示例 - 在异步环境中使用同步方法
    """
    print("\n=== 混合使用示例 ===")
    
    async def mixed_operations():
        cache = AsyncFileCache()
        
        # 混合使用同步和异步方法
        await cache.set("mixed_key1", "mixed_value1")
        cache["mixed_key2"] = "mixed_value2"  # 同步方法
        
        # 异步获取
        value1 = await cache.get("mixed_key1")
        print(f"async get: {value1}")
        
        # 同步获取
        value2 = cache["mixed_key2"]
        print(f"sync get: {value2}")
        
        # 同步检查
        exists = "mixed_key1" in cache
        print(f"sync contains: {exists}")
        
        await cache.close()
    
    asyncio.run(mixed_operations())


def error_handling_example():
    """
    错误处理示例
    """
    print("\n=== 错误处理示例 ===")
    
    cache = AsyncFileCache()
    
    try:
        # 尝试获取不存在的键
        value = cache["non_existent_key"]
    except KeyError as e:
        print(f"KeyError caught: {e}")
    
    try:
        # 尝试删除不存在的键
        del cache["non_existent_key"]
    except KeyError as e:
        print(f"KeyError caught: {e}")
    
    asyncio.run(cache.close())


def performance_comparison():
    """
    性能对比示例
    """
    print("\n=== 性能对比示例 ===")
    
    async def performance_test():
        cache = AsyncFileCache()
        
        # 测试异步方法性能
        import time
        
        # 异步方法
        start = time.time()
        for i in range(100):
            await cache.set(f"async_key_{i}", f"value_{i}")
        for i in range(100):
            await cache.get(f"async_key_{i}")
        async_time = time.time() - start
        print(f"Async operations: {async_time:.4f}s")
        
        # 同步方法
        start = time.time()
        for i in range(100):
            cache[f"sync_key_{i}"] = f"value_{i}"
        for i in range(100):
            _ = cache[f"sync_key_{i}"]
        sync_time = time.time() - start
        print(f"Sync operations: {sync_time:.4f}s")
        
        await cache.close()
    
    asyncio.run(performance_test())


if __name__ == "__main__":
    # 运行所有示例
    asyncio.run(async_usage_example())
    sync_usage_example()
    mixed_usage_example()
    error_handling_example()
    performance_comparison()
    
    print("\n=== 总结 ===")
    print("1. 在异步环境中，优先使用异步方法（get, set, exists等）")
    print("2. 在同步环境中，可以使用同步的dict-like方法（__getitem__, __setitem__等）")
    print("3. 同步方法内部会创建事件循环来调用异步方法")
    print("4. 混合使用是安全的，但要注意性能影响")
    print("5. 错误处理与标准dict行为一致")