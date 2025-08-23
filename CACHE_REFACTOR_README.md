# 缓存模块重构：基于diskcache实现

## 概述

本次重构将 `app.core.cache` 模块中的 `FileBackend` 和 `AsyncFileBackend` 实现从基于文件系统改为基于 `diskcache` 库实现，提供了更好的性能和功能。

## 主要变更

### 1. 依赖更新

在 `requirements.in` 中添加了 `diskcache~=6.1.0` 依赖。

### 2. 类实现重构

#### FileBackend 类
- **原实现**: 基于文件系统，直接操作文件
- **新实现**: 基于 `diskcache.Cache`，提供更好的性能和功能

#### AsyncFileBackend 类  
- **原实现**: 基于异步文件操作
- **新实现**: 基于 `diskcache.Cache`，保持异步接口兼容性

### 3. 新增功能

#### 缓存大小限制
```python
# 设置1GB缓存大小限制
cache = FileBackend(base=Path("/tmp/cache"), size_limit=1024*1024*1024)
```

#### 缓存淘汰策略
```python
# 使用最少使用频率淘汰策略
cache = FileBackend(
    base=Path("/tmp/cache"), 
    eviction_policy='least-frequently-used'
)
```

#### TTL支持
```python
# 设置10秒过期时间
cache.set("key", "value", ttl=10, region="my_region")
```

### 4. 工厂函数更新

#### FileCache 函数
```python
def FileCache(base: Path = settings.TEMP_PATH, ttl: Optional[int] = None, 
              size_limit: Optional[int] = None, eviction_policy: str = 'least-recently-used') -> CacheBackend:
```

#### AsyncFileCache 函数
```python
def AsyncFileCache(base: Path = settings.TEMP_PATH, ttl: Optional[int] = None,
                   size_limit: Optional[int] = None, eviction_policy: str = 'least-recently-used') -> AsyncCacheBackend:
```

## 优势

### 1. 性能提升
- **diskcache** 使用 SQLite 数据库存储，比文件系统操作更高效
- 支持内存缓存和磁盘缓存的混合模式
- 更好的并发性能

### 2. 功能增强
- **自动过期**: 支持 TTL (Time To Live) 自动过期
- **大小限制**: 可设置缓存总大小限制
- **淘汰策略**: 支持 LRU (Least Recently Used) 和 LFU (Least Frequently Used) 淘汰策略
- **原子操作**: 所有操作都是原子的，避免并发问题

### 3. 可靠性
- **事务支持**: 基于 SQLite 的事务机制
- **数据完整性**: 更好的数据一致性保证
- **错误恢复**: 更好的错误处理和恢复机制

## 兼容性

### 接口兼容
- 所有原有的公共接口保持不变
- 现有的代码无需修改即可使用新实现

### 配置兼容
- 当 `CACHE_BACKEND_TYPE` 设置为 "redis" 时，仍使用 Redis 后端
- 当设置为其他值时，使用新的 diskcache 后端

## 使用示例

### 基本使用
```python
from app.core.cache import FileCache, AsyncFileCache
from pathlib import Path

# 同步缓存
cache = FileCache(base=Path("/tmp/cache"))
cache.set("key", "value", region="my_region")
value = cache.get("key", region="my_region")

# 异步缓存
async_cache = AsyncFileCache(base=Path("/tmp/cache"))
await async_cache.set("key", "value", region="my_region")
value = await async_cache.get("key", region="my_region")
```

### 高级配置
```python
# 带大小限制和淘汰策略的缓存
cache = FileCache(
    base=Path("/tmp/cache"),
    size_limit=1024*1024*1024,  # 1GB
    eviction_policy='least-frequently-used'
)

# 设置带TTL的缓存
cache.set("temp_key", "temp_value", ttl=3600, region="temp_region")  # 1小时过期
```

### 缓存管理
```python
# 检查缓存是否存在
if cache.exists("key", region="my_region"):
    value = cache.get("key", region="my_region")

# 删除特定缓存
cache.delete("key", region="my_region")

# 清空区域缓存
cache.clear(region="my_region")

# 获取所有缓存项
for key, value in cache.items(region="my_region"):
    print(f"{key}: {value}")

# 关闭缓存
cache.close()
```

## 测试

运行测试脚本验证实现：
```bash
python test_diskcache.py
```

## 注意事项

1. **首次使用**: 首次使用时会自动创建缓存目录和数据库文件
2. **权限要求**: 确保应用有权限在指定目录创建和写入文件
3. **磁盘空间**: 注意监控缓存目录的磁盘使用情况
4. **性能调优**: 根据实际使用情况调整 `size_limit` 和 `eviction_policy`

## 迁移指南

### 现有代码
现有使用 `FileCache` 和 `AsyncFileCache` 的代码无需修改，会自动使用新的 diskcache 实现。

### 配置更新
如果需要使用新功能，可以更新缓存初始化代码：

```python
# 旧代码
cache = FileCache()

# 新代码（可选，使用新功能）
cache = FileCache(
    size_limit=1024*1024*1024,  # 1GB限制
    eviction_policy='least-frequently-used'
)
```

## 总结

这次重构显著提升了文件缓存的性能和功能，同时保持了完全的向后兼容性。新的实现基于成熟的 diskcache 库，提供了更好的可靠性、性能和功能特性。