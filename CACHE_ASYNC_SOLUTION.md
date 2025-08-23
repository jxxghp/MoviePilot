# AsyncCacheBackend Dict-like方法异步支持解决方案

## 问题描述

Python的魔术方法（如`__getitem__`、`__setitem__`、`__delitem__`等）**不能是异步的**。这是Python语言本身的限制。当你尝试使用`async def __getitem__`时，Python解释器不会将其识别为标准的魔术方法。

## 解决方案

### 方案1：同步包装器（已实现）

在`AsyncCacheBackend`中，我们已经实现了同步的dict-like方法，内部调用异步方法：

```python
def __getitem__(self, key: str) -> Any:
    """获取缓存项，类似 dict[key]（同步包装器）"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    value = loop.run_until_complete(self.get(key))
    if value is None:
        raise KeyError(key)
    return value
```

**优点：**
- 可以直接使用`cache["key"]`语法
- 与标准dict行为一致
- 向后兼容

**缺点：**
- 在异步环境中会阻塞事件循环
- 性能可能不如直接使用异步方法

### 方案2：异步方法（已实现）

提供显式的异步方法：

```python
async def async_getitem(self, key: str) -> Any:
    """获取缓存项，类似 dict[key]（异步）"""
    value = await self.get(key)
    if value is None:
        raise KeyError(key)
    return value
```

**优点：**
- 真正的异步操作
- 不会阻塞事件循环
- 性能更好

**缺点：**
- 需要使用`await cache.async_getitem("key")`
- 语法不如dict-like方法简洁

### 方案3：上下文管理器（推荐）

使用`AsyncCacheContext`提供更优雅的使用方式：

```python
from app.core.cache_utils import create_cache_context

# 异步使用
async with create_cache_context(cache) as ctx:
    ctx["key"] = "value"
    value = ctx["key"]

# 同步使用
with create_cache_context(cache) as ctx:
    ctx["key"] = "value"
    value = ctx["key"]
```

### 方案4：异步字典包装器

使用`AsyncCacheDict`提供真正的异步dict-like接口：

```python
from app.core.cache_utils import create_async_dict

async_dict = create_async_dict(cache)
await async_dict["key"] = "value"
value = await async_dict["key"]
```

## 使用建议

### 1. 异步环境中的最佳实践

```python
# 推荐：直接使用异步方法
async def async_function():
    cache = AsyncFileCache()
    await cache.set("key", "value")
    value = await cache.get("key")
    await cache.close()

# 推荐：使用异步字典包装器
async def async_function_with_dict():
    cache = AsyncFileCache()
    async_dict = create_async_dict(cache)
    await async_dict["key"] = "value"
    value = await async_dict["key"]
    await cache.close()

# 推荐：使用上下文管理器
async def async_function_with_context():
    cache = AsyncFileCache()
    async with create_cache_context(cache) as ctx:
        ctx["key"] = "value"
        value = ctx["key"]
```

### 2. 同步环境中的使用

```python
# 可以使用同步包装器
def sync_function():
    cache = AsyncFileCache()
    cache["key"] = "value"
    value = cache["key"]
    asyncio.run(cache.close())

# 推荐：使用上下文管理器
def sync_function_with_context():
    cache = AsyncFileCache()
    with create_cache_context(cache) as ctx:
        ctx["key"] = "value"
        value = ctx["key"]
```

### 3. 混合使用

```python
async def mixed_function():
    cache = AsyncFileCache()
    
    # 异步操作
    await cache.set("async_key", "async_value")
    
    # 同步操作（在需要时）
    with create_cache_context(cache) as ctx:
        ctx["sync_key"] = "sync_value"
        value = ctx["sync_key"]
    
    await cache.close()
```

## 性能考虑

### 1. 异步 vs 同步性能

```python
# 异步操作（推荐）
async def async_operations():
    cache = AsyncFileCache()
    for i in range(1000):
        await cache.set(f"key_{i}", f"value_{i}")
    await cache.close()

# 同步操作（可能较慢）
def sync_operations():
    cache = AsyncFileCache()
    for i in range(1000):
        cache[f"key_{i}"] = f"value_{i}"  # 内部会创建事件循环
    asyncio.run(cache.close())
```

### 2. 批量操作优化

```python
async def batch_operations():
    cache = AsyncFileCache()
    
    # 批量设置
    tasks = []
    for i in range(1000):
        task = cache.set(f"key_{i}", f"value_{i}")
        tasks.append(task)
    
    await asyncio.gather(*tasks)
    await cache.close()
```

## 错误处理

### 1. 键不存在的情况

```python
# 异步方法
try:
    value = await cache.get("non_existent_key")
    if value is None:
        print("Key not found")
except Exception as e:
    print(f"Error: {e}")

# 同步方法
try:
    value = cache["non_existent_key"]
except KeyError as e:
    print(f"KeyError: {e}")
```

### 2. 网络错误处理

```python
async def robust_cache_operation():
    cache = AsyncFileCache()
    try:
        await cache.set("key", "value")
        value = await cache.get("key")
    except Exception as e:
        logger.error(f"Cache operation failed: {e}")
        # 降级处理
        value = "fallback_value"
    finally:
        await cache.close()
```

## 最佳实践总结

1. **在异步环境中**：优先使用异步方法或`AsyncCacheDict`
2. **在同步环境中**：使用`AsyncCacheContext`或同步包装器
3. **性能敏感场景**：避免在异步环境中使用同步包装器
4. **错误处理**：始终处理可能的异常
5. **资源管理**：使用上下文管理器确保资源正确释放
6. **批量操作**：使用`asyncio.gather`进行并发操作

## 迁移指南

如果你现有的代码使用了异步的dict-like方法，需要迁移：

```python
# 旧代码（不工作）
async def old_code():
    cache = AsyncFileCache()
    value = await cache["key"]  # 这会失败

# 新代码（推荐）
async def new_code():
    cache = AsyncFileCache()
    value = await cache.get("key")  # 使用异步方法
    # 或者
    async_dict = create_async_dict(cache)
    value = await async_dict["key"]  # 使用异步字典包装器
```

通过这些解决方案，你可以灵活地在不同场景下使用AsyncCacheBackend，同时保持代码的可读性和性能。