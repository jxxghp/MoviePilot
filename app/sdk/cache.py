"""插件可用的缓存契约、适配器工厂和装饰器。"""

from app.adapters.cache.backends import (
    AsyncFileBackend,
    FileBackend,
)
from app.runtime.cache import (
    AsyncCache,
    AsyncCacheBackend,
    AsyncFileCache,
    AsyncMemoryBackend,
    AtomicCacheBackend,
    Cache,
    CacheBackend,
    FileCache,
    LRUCache,
    MemoryBackend,
    TTLCache,
    async_fresh,
    cached,
    fresh,
    is_fresh,
)

# RedisBackend / AsyncRedisBackend 由兼容层惰性解析到 app.adapters.cache.redis，显式导入仍可用；
# 不列入星号导出，避免文件缓存实例因 ``import *`` 加载 redis 客户端库。
__all__ = [
    "AtomicCacheBackend",
    "AsyncCache",
    "AsyncCacheBackend",
    "AsyncFileBackend",
    "AsyncFileCache",
    "AsyncMemoryBackend",
    "Cache",
    "CacheBackend",
    "FileBackend",
    "FileCache",
    "LRUCache",
    "MemoryBackend",
    "TTLCache",
    "async_fresh",
    "cached",
    "fresh",
    "is_fresh",
]
