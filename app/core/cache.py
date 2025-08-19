# 导入新的RedisHelper
from app.helper.redis_helper import redis_helper, cached, close_redis_cache

# 为了向后兼容，保留一些旧的导入
from app.helper.redis_helper import CacheBackend, CacheToolsBackend, RedisBackend, get_cache_backend

# 缓存后端实例（为了向后兼容）
cache_backend = redis_helper._cache_backend


def close_cache() -> None:
    """
    关闭缓存后端连接并清理资源
    """
    close_redis_cache()
