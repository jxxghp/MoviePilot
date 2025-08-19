# 缓存系统重构说明

## 概述

本次重构将Redis相关内容从`core/cache.py`中抽离，创建了统一的缓存管理系统，支持Redis和本地文件缓存的混合使用。

## 重构内容

### 1. 新增文件

#### `app/helper/redis_helper.py`
- **RedisHelper类**: 统一的Redis缓存助手类
- **CacheBackend基类**: 缓存后端基类，定义通用接口
- **CacheToolsBackend类**: 基于cachetools的内存缓存后端
- **RedisBackend类**: 基于Redis的缓存后端
- **cached装饰器**: 支持同步和异步函数的缓存装饰器

#### `app/helper/cache_manager.py`
- **CacheManager类**: 统一的缓存管理器，支持Redis和本地文件缓存的混合使用
- **TmdbCacheManager类**: TMDB专用缓存管理器
- **DoubanCacheManager类**: 豆瓣专用缓存管理器
- **TorrentsCacheManager类**: 种子专用缓存管理器

### 2. 修改文件

#### `app/core/cache.py`
- 删除了重复的类定义
- 保留向后兼容的导入和接口
- 简化了文件结构

#### `app/modules/themoviedb/tmdb_cache.py`
- 重构为使用新的CacheManager
- 移除了本地文件操作逻辑
- 支持Redis和本地文件缓存的混合使用

#### `app/modules/douban/douban_cache.py`
- 重构为使用新的CacheManager
- 移除了本地文件操作逻辑
- 支持Redis和本地文件缓存的混合使用

#### `app/chain/torrents.py`
- 重构种子缓存逻辑
- 使用新的TorrentsCacheManager
- 支持Redis和本地文件缓存的混合使用

#### `app/helper/__init__.py`
- 导出新的缓存模块

## 功能特性

### 1. 混合缓存策略
- **优先使用Redis**: 当Redis可用时，优先使用Redis缓存
- **自动回退**: Redis不可用时，自动回退到本地文件缓存
- **数据同步**: 同时写入Redis和本地文件，确保数据一致性

### 2. 缓存区域管理
- **themoviedb**: TMDB相关缓存
- **douban**: 豆瓣相关缓存
- **torrents_spider**: 种子爬虫缓存
- **torrents_rss**: 种子RSS缓存

### 3. 过期时间管理
- 支持TTL（Time To Live）设置
- 自动清理过期缓存
- 可配置的默认过期时间

### 4. 向后兼容
- 保留原有的API接口
- 支持现有的缓存装饰器
- 平滑迁移，不影响现有功能

## 配置说明

### Redis配置
在`app/core/config.py`中配置：

```python
# 缓存类型，支持 cachetools 和 redis
CACHE_BACKEND_TYPE: str = "cachetools"

# Redis连接字符串
CACHE_BACKEND_URL: Optional[str] = None

# Redis最大内存限制
CACHE_REDIS_MAXMEMORY: Optional[str] = None
```

### 使用示例

#### 1. 使用RedisHelper
```python
from app.helper.redis_helper import redis_helper

# 设置缓存
redis_helper.set("key", "value", ttl=3600, region="my_region")

# 获取缓存
value = redis_helper.get("key", region="my_region")

# 检查缓存是否存在
exists = redis_helper.exists("key", region="my_region")
```

#### 2. 使用缓存装饰器
```python
from app.helper.redis_helper import cached

@cached(region="my_region", ttl=1800)
def my_function(param1, param2):
    # 复杂的计算逻辑
    return result
```

#### 3. 使用CacheManager
```python
from app.helper.cache_manager import CacheManager

# 创建缓存管理器
cache = CacheManager("my_cache", region="my_region")

# 设置缓存
cache.set("key", "value", ttl=3600)

# 获取缓存
value = cache.get("key")
```

#### 4. 使用专用缓存管理器
```python
from app.helper.cache_manager import tmdb_cache_manager, douban_cache_manager

# TMDB缓存
tmdb_cache_manager.set_by_meta(meta, data)
data = tmdb_cache_manager.get_by_meta(meta)

# 豆瓣缓存
douban_cache_manager.set_by_meta(meta, data)
data = douban_cache_manager.get_by_meta(meta)
```

## 测试

运行测试脚本验证重构：

```bash
python test_cache_refactor.py
```

## 迁移指南

### 1. 现有代码兼容性
- 现有的`@cached`装饰器继续有效
- 现有的`cache_backend`实例继续可用
- 现有的缓存配置无需修改

### 2. 推荐迁移步骤
1. 更新导入语句，使用新的缓存模块
2. 逐步替换直接的文件缓存操作为CacheManager
3. 测试缓存功能是否正常
4. 清理旧的缓存文件

### 3. 注意事项
- 确保Redis配置正确（如果使用Redis）
- 检查缓存文件路径权限
- 监控缓存性能指标
- 定期清理过期缓存

## 性能优化

### 1. Redis优化
- 使用连接池
- 配置合适的内存限制
- 设置合理的过期时间

### 2. 本地文件优化
- 定期清理过期缓存
- 使用压缩存储
- 优化文件读写频率

### 3. 缓存策略
- 根据访问频率设置不同的TTL
- 使用LRU淘汰策略
- 实现缓存预热机制

## 故障排除

### 1. Redis连接问题
- 检查Redis服务状态
- 验证连接字符串格式
- 确认网络连通性

### 2. 缓存数据不一致
- 检查Redis和本地文件同步
- 验证缓存键生成逻辑
- 确认过期时间设置

### 3. 性能问题
- 监控缓存命中率
- 分析缓存大小
- 优化缓存策略

## 总结

本次重构实现了：
1. **模块化设计**: 将Redis功能抽离到独立的模块
2. **统一接口**: 提供一致的缓存操作接口
3. **混合缓存**: 支持Redis和本地文件的混合使用
4. **向后兼容**: 保持现有代码的兼容性
5. **易于维护**: 简化了缓存逻辑，提高了代码可维护性

重构后的缓存系统更加灵活、可靠，能够更好地满足不同场景的需求。