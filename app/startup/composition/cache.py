"""缓存 Adapter 的宿主组合根。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.adapters.cache.backends import configure_platform_cache
from app.runtime.settings import get_runtime_setting

# 未选用 Redis 时的本地缓存类型；平台缓存只区分 redis 与非 redis 两条路由。
_LOCAL_BACKEND_TYPE = "cachetools"


@dataclass(frozen=True, slots=True)
class CacheComposition:
    """记录本进程启动时选定的缓存实现，供关闭与配置重载定位连接 owner。"""

    redis_enabled: bool  # 启动时 CACHE_BACKEND_TYPE 是否为 redis
    sync_redis_owner: Optional[type[Any]] = None  # 同步 Redis 连接 Singleton 类型，未启用时为空
    async_redis_owner: Optional[type[Any]] = None  # 异步 Redis 连接 Singleton 类型，未启用时为空

    @property
    def redis_owners(self) -> tuple[type[Any], ...]:
        """返回已接入的 Redis 连接 owner 类型。"""
        return tuple(
            owner
            for owner in (self.sync_redis_owner, self.async_redis_owner)
            if owner is not None
        )


_composition = CacheComposition(redis_enabled=False)


def configure_cache_composition() -> None:
    """在业务模块导入前登记平台缓存具体实现。

    缓存设置属于重启生效的配置组，因此只在启动时按 ``CACHE_BACKEND_TYPE`` 选一次实现。
    选用 Redis 时才导入 Redis 适配器，文件缓存实例不加载 redis 客户端库。启动时未接入
    Redis 的进程在运行中改成 redis 会继续使用本地缓存，直到重启；已接入 Redis 的进程
    在运行中改回本地缓存则立即切换，与之前一致。
    """
    global _composition
    if get_runtime_setting("CACHE_BACKEND_TYPE") != "redis":
        configure_platform_cache(
            backend_type_provider=_local_backend_type,
        )
        _composition = CacheComposition(redis_enabled=False)
        return

    from app.adapters.cache.redis import (
        AsyncRedisBackend,
        AsyncRedisHelper,
        RedisBackend,
        RedisHelper,
    )

    configure_platform_cache(
        backend_type_provider=lambda: get_runtime_setting("CACHE_BACKEND_TYPE"),
        redis_factory=lambda ttl: RedisBackend(ttl=ttl),
        async_redis_factory=lambda ttl: AsyncRedisBackend(ttl=ttl),
        redis_probe=lambda: RedisHelper().test(),
    )
    _composition = CacheComposition(
        redis_enabled=True,
        sync_redis_owner=RedisHelper,
        async_redis_owner=AsyncRedisHelper,
    )


def get_cache_composition() -> CacheComposition:
    """返回本进程当前的缓存组合结果。"""
    return _composition


def _local_backend_type() -> str:
    """启动时未接入 Redis，运行中的 redis 设置在重启前不生效。"""
    backend_type = get_runtime_setting("CACHE_BACKEND_TYPE")
    return _LOCAL_BACKEND_TYPE if backend_type == "redis" else backend_type
