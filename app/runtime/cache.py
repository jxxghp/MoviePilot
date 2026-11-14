"""
缓存契约、进程内后端与装饰器，具体持久化后端由启动层注入。
"""

import asyncio
import contextvars
import inspect
import logging
import threading
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Coroutine,
    Dict,
    Generator,
    Literal,
    Optional,
    Protocol,
    Tuple,
    TypedDict,
    TypeVar,
    Union,
    Unpack,
    cast,
)

from cachetools import LRUCache as MemoryLRUCache
from cachetools import TLRUCache as MemoryTLRUCache
from cachetools.keys import hashkey

# 默认缓存区
DEFAULT_CACHE_REGION = "DEFAULT"
# 默认内存缓存上限，单位为条目
DEFAULT_CACHE_SIZE = 1024
# 默认缓存有效期，单位为秒，按 365 天设置
DEFAULT_CACHE_TTL = 365 * 24 * 60 * 60
logger = logging.getLogger(__name__)

_backend_type_provider: Callable[[], str] = lambda: "memory"
_redis_factory: Optional[Callable[[Optional[int]], "AtomicCacheBackend"]] = None
_async_redis_factory: Optional[
    Callable[[Optional[int]], "AsyncCacheBackend"]
] = None
_file_factory: Optional[Callable[[Optional[Path]], "CacheBackend"]] = None
_async_file_factory: Optional[
    Callable[[Optional[Path]], "AsyncCacheBackend"]
] = None
_file_ttl_provider: Callable[[], int] = lambda: DEFAULT_CACHE_TTL
_redis_probe: Optional[Callable[[], bool]] = None


# 多文件业务缓存的读改写必须共享同一进程锁，锁名由有限的宿主缓存合同提供。
_mutation_locks: dict[str, threading.Lock] = {}
_mutation_locks_guard = threading.Lock()


def _cache_mutation_lock(name: str) -> threading.Lock:
    """
    获取同步线程与异步任务共用的业务缓存写锁。

    :param name: 宿主定义的有限业务缓存名称
    :return: 当前进程该名称唯一的锁
    """
    with _mutation_locks_guard:
        return _mutation_locks.setdefault(name, threading.Lock())


@contextmanager
def cache_mutation(name: str) -> Generator[None, None, None]:
    """
    在同步调用中串行化一个业务缓存的完整读改写。

    :param name: 宿主定义的有限业务缓存名称
    :yields: 本次读改写持有锁的作用域
    """
    with _cache_mutation_lock(name):
        yield


@asynccontextmanager
async def async_cache_mutation(name: str) -> AsyncGenerator[None, None]:
    """
    非阻塞等待共享写锁，取消后不会有后台线程迟到取得锁。

    :param name: 与同步调用共用的业务缓存名称
    :yields: 本次异步读改写持有锁的作用域
    """
    lock = _cache_mutation_lock(name)
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.01)
    try:
        yield
    finally:
        lock.release()


def configure_cache_factories(
    *,
    backend_type_provider: Callable[[], str],
    file_factory: Callable[[Optional[Path]], "CacheBackend"],
    async_file_factory: Callable[[Optional[Path]], "AsyncCacheBackend"],
    file_ttl_provider: Callable[[], int],
    redis_factory: Optional[Callable[[Optional[int]], "AtomicCacheBackend"]] = None,
    async_redis_factory: Optional[Callable[[Optional[int]], "AsyncCacheBackend"]] = None,
    redis_probe: Optional[Callable[[], bool]] = None,
) -> None:
    """
    注入配置读取器和具体缓存适配器工厂。

    Redis 工厂与探测器只在启动层选用 Redis 缓存时传入；此时 ``backend_type_provider``
    才可能返回 ``redis``，未接入 Redis 的进程不会走到 Redis 分支。
    """
    global _backend_type_provider, _redis_factory, _async_redis_factory
    global _file_factory, _async_file_factory, _file_ttl_provider, _redis_probe
    _backend_type_provider = backend_type_provider
    _redis_factory = redis_factory
    _async_redis_factory = async_redis_factory
    _file_factory = file_factory
    _async_file_factory = async_file_factory
    _file_ttl_provider = file_ttl_provider
    _redis_probe = redis_probe


def probe_redis_cache() -> Optional[bool]:
    """
    探测启动时接入的 Redis 缓存是否可连接；本进程未接入 Redis 时返回 None。
    """
    if _redis_probe is None:
        return None
    return _redis_probe()


# 上下文变量来控制缓存行为
_fresh = contextvars.ContextVar('fresh', default=False)


class _RegionOptions(TypedDict, total=False):
    """
    缓存代理传给后端的区域选项。

    :ivar region: 显式覆盖代理绑定区域；None 保留后端默认区域语义
    """

    region: Optional[str]


class _WriteOptions(_RegionOptions, total=False):
    """
    缓存写入及批量写入选项。

    :ivar ttl: 当前条目的有效期，单位秒；None 沿用后端默认值
    :ivar maxsize: 内存区域首次创建时的最大容量；None 沿用默认容量
    """

    ttl: Optional[int]
    maxsize: Optional[int]


class CacheBackend(ABC):
    """
    缓存后端基类，定义通用的缓存接口。
    """

    def __getitem__(self, key: str) -> Any:
        """
        获取缓存项，类似 dict[key]。
        """
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        """
        设置缓存项，类似 dict[key] = value。
        """
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        """
        删除缓存项，类似 del dict[key]。
        """
        if not self.exists(key):
            raise KeyError(key)
        self.delete(key)

    def __contains__(self, key: str) -> bool:
        """
        检查键是否存在，类似 key in dict。
        """
        return self.exists(key)

    def __iter__(self) -> Generator[str, None, None]:
        """
        返回缓存的迭代器，类似 iter(dict)。
        """
        for key, _ in self.items():
            yield key

    def __len__(self) -> int:
        """
        返回缓存项的数量，类似 len(dict)。
        """
        return sum(1 for _ in self.items())

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs: object) -> None:
        """
        设置缓存。

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，单位秒
        :param region: 缓存的区
        :param kwargs: 其他参数
        """
        pass

    @abstractmethod
    def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在。

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        pass

    @abstractmethod
    def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存。

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        pass

    @abstractmethod
    def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存。

        :param key: 缓存的键
        :param region: 缓存的区
        """
        pass

    @abstractmethod
    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存。

        :param region: 缓存的区，为None时清空所有区缓存
        """
        pass

    @abstractmethod
    def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[Tuple[str, Any], None, None]:
        """
        获取指定区域的所有缓存项。

        :param region: 缓存的区
        :yields: 当前缓存区域中的键值对，每次产生一个二元组
        """
        pass

    def keys(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[str, None, None]:
        """
        获取所有缓存键，类似 dict.keys()。
        """
        for key, _ in self.items(region=region):
            yield key

    def values(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[Any, None, None]:
        """
        获取所有缓存值，类似 dict.values()。
        """
        for _, value in self.items(region=region):
            yield value

    def update(self, other: Dict[str, Any], region: Optional[str] = DEFAULT_CACHE_REGION,
               ttl: Optional[int] = None, **kwargs: object) -> None:
        """
        更新缓存，类似 dict.update()。
        """
        for key, value in other.items():
            self.set(key, value, ttl=ttl, region=region, **kwargs)

    def pop(self, key: str, default: Any = None, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        弹出缓存项，类似 dict.pop()。
        """
        value = self.get(key, region=region)
        if value is not None:
            self.delete(key, region=region)
            return value
        if default is not None:
            return default
        raise KeyError(key)

    def popitem(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Tuple[str, Any]:
        """
        弹出最后一个缓存项，类似 dict.popitem()。
        """
        items = list(self.items(region=region))
        if not items:
            raise KeyError("popitem(): cache is empty")
        key, value = items[-1]
        self.delete(key, region=region)
        return key, value

    def setdefault(self, key: str, default: Any = None, region: Optional[str] = DEFAULT_CACHE_REGION,
                   ttl: Optional[int] = None, **kwargs: object) -> Any:
        """
        设置默认值，类似 dict.setdefault()。
        """
        value = self.get(key, region=region)
        if value is None:
            self.set(key, default, ttl=ttl, region=region, **kwargs)
            return default
        return value

    @abstractmethod
    def close(self) -> None:
        """
        关闭缓存连接。
        """
        pass

    @staticmethod
    def get_region(region: Optional[str] = None) -> str:
        """
        获取缓存的区。
        """
        return f"region:{region}" if region else "region:DEFAULT"

    @staticmethod
    def is_redis() -> bool:
        """
        判断当前缓存后端是否为 Redis。
        """
        return False


class AtomicCacheBackend(CacheBackend):
    """
    支持严格写入和原子领取的一次性缓存后端契约。
    """

    @abstractmethod
    def store(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **kwargs: Any,
    ) -> None:
        """
        写入缓存；后端故障必须向调用方传播。
        """

    @abstractmethod
    def consume(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Any:
        """
        原子读取并删除缓存值，不存在时返回 None。
        """

    def pop(
        self,
        key: str,
        default: Any = None,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Any:
        """
        以原子领取实现兼容的字典 pop 语义。
        """
        value = self.consume(key=key, region=region)
        if value is not None:
            return value
        if default is not None:
            return default
        raise KeyError(key)


class AsyncCacheBackend(CacheBackend):
    """
    异步缓存契约，保留插件依赖的同步基类继承关系。

    异步返回值无法满足同步父类的返回合同，兼容例外仅登记对应方法签名。
    """

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: Optional[int] = None,  # type: ignore[override]
                  region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs: object) -> None:
        """
        设置缓存。

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，单位秒
        :param region: 缓存的区
        :param kwargs: 其他参数
        """
        pass

    @abstractmethod
    async def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:  # type: ignore[override]
        """
        判断缓存键是否存在。

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        pass

    @abstractmethod
    async def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存。

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        pass

    @abstractmethod
    async def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:  # type: ignore[override]
        """
        删除缓存。

        :param key: 缓存的键
        :param region: 缓存的区
        """
        pass

    @abstractmethod
    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:  # type: ignore[override]
        """
        清除指定区域的缓存或全部缓存。

        :param region: 缓存的区，为None时清空所有区缓存
        """
        pass

    @abstractmethod
    def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[Tuple[str, Any], None]:  # type: ignore[override]
        """
        获取指定区域的所有缓存项。

        :param region: 缓存的区域
        :yields: 当前缓存区域中的键值对，每次产生一个二元组
        """
        pass

    async def keys(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[str, None]:  # type: ignore[override]
        """
        获取所有缓存键，类似 dict.keys()（异步）。
        """
        async for key, _ in self.items(region=region):
            yield key

    async def values(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[Any, None]:  # type: ignore[override]
        """
        获取所有缓存值，类似 dict.values()（异步）。
        """
        async for _, value in self.items(region=region):
            yield value

    async def update(self, other: Dict[str, Any], region: Optional[str] = DEFAULT_CACHE_REGION,  # type: ignore[override]
                     ttl: Optional[int] = None, **kwargs: object) -> None:
        """
        更新缓存，类似 dict.update()（异步）。
        """
        for key, value in other.items():
            await self.set(key, value, ttl=ttl, region=region, **kwargs)

    async def pop(self, key: str, default: Any = None, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        弹出缓存项，类似 dict.pop()（异步）。
        """
        value = await self.get(key, region=region)
        if value is not None:
            await self.delete(key, region=region)
            return value
        if default is not None:
            return default
        raise KeyError(key)

    async def popitem(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Tuple[str, Any]:  # type: ignore[override]
        """
        弹出最后一个缓存项，类似 dict.popitem()（异步）。
        """
        items = []
        async for item in self.items(region=region):
            items.append(item)
        if not items:
            raise KeyError("popitem(): cache is empty")
        key, value = items[-1]
        await self.delete(key, region=region)
        return key, value

    async def setdefault(self, key: str, default: Any = None, region: Optional[str] = DEFAULT_CACHE_REGION,
                         ttl: Optional[int] = None, **kwargs: object) -> Any:
        """
        设置默认值，类似 dict.setdefault()（异步）。
        """
        value = await self.get(key, region=region)
        if value is None:
            await self.set(key, default, ttl=ttl, region=region, **kwargs)
            return default
        return value

    @abstractmethod
    async def close(self) -> None:  # type: ignore[override]
        """
        关闭缓存连接。
        """
        pass


class _MemoryTLRUCache(MemoryTLRUCache[str, Any]):
    """
    支持为每个 key 设置独立 TTL 的内存缓存。

    :ivar __ttl: 未指定单键存活时间时采用的秒数
    :ivar __setting_ttls: 写入期间临时保存各键的存活秒数，完成写入后移除
    """

    def __init__(self, maxsize: int, ttl: int):
        """
        使用默认 TTL 初始化支持单键过期时间的内存缓存。
        """
        self.__ttl = ttl
        self.__setting_ttls: Dict[str, int] = {}
        super().__init__(maxsize=maxsize, ttu=self._get_expiration)

    def _get_expiration(self, key: str, _value: Any, now: float) -> float:
        """
        计算指定键的绝对过期时间。
        """
        return now + self.__setting_ttls.get(key, self.__ttl)

    @property
    def ttl(self) -> int:
        """
        默认缓存存活时间，单位秒。
        """
        return self.__ttl

    def set(self, key: str, value: Any, ttl: int) -> None:
        """
        使用指定 TTL 设置缓存值。
        """
        if ttl <= 0:
            try:
                del self[key]
            except KeyError:
                pass
            return
        self.__setting_ttls[key] = ttl
        try:
            super().__setitem__(key, value)
        finally:
            self.__setting_ttls.pop(key, None)


class MemoryBackend(AtomicCacheBackend):
    """
    基于 `cachetools.TLRUCache` 实现的缓存后端。

    :cvar _lock: 保护所有区域缓存写入及快照读取的进程锁
    :cvar _region_caches: 按区域保存共享的有界内存缓存
    :ivar cache_type: 当前实例创建区域时采用的淘汰策略
    :ivar maxsize: 新建区域时的默认容量
    :ivar ttl: 未显式指定单键存活时间时使用的秒数
    """

    # 类变量 _region_caches 的互斥锁
    _lock = threading.Lock()
    # 存储各个 region 的缓存实例，region -> TLRUCache/LRUCache
    _region_caches: Dict[str, Union[_MemoryTLRUCache, MemoryLRUCache[str, Any]]] = {}

    def __init__(self, cache_type: Literal['ttl', 'lru'] = 'ttl',
                 maxsize: Optional[int] = None, ttl: Optional[int] = None):
        """
        初始化缓存实例。

        :param cache_type: 缓存类型，支持 'ttl'（默认）和 'lru'
        :param maxsize: 缓存的最大条目数
        :param ttl: 默认缓存存活时间，单位秒
        """
        self.cache_type = cache_type
        self.maxsize = maxsize or DEFAULT_CACHE_SIZE
        self.ttl = DEFAULT_CACHE_TTL if ttl is None else ttl

    def __get_region_cache(self, region: Optional[str]) -> Optional[Union[_MemoryTLRUCache, MemoryLRUCache[str, Any]]]:
        """
        获取指定区域的缓存实例，如果不存在则返回 None。
        """
        region = self.get_region(region)
        return self._region_caches.get(region)

    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs: object) -> None:
        """
        设置缓存值支持每个 key 独立配置 TTL。

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，未传入则使用 backend 默认值，单位秒
        :param region: 缓存的区
        """
        ttl = self.ttl if ttl is None else ttl
        # maxsize 按公开写入契约传入整数容量，保留原有真值回退。
        maxsize = cast(Optional[int], kwargs.get("maxsize")) or self.maxsize
        region = self.get_region(region)
        with self._lock:
            region_cache = self._region_caches.get(region)
            if region_cache is None:
                region_cache = (
                    _MemoryTLRUCache(maxsize=maxsize, ttl=ttl) if self.cache_type == 'ttl'
                    else MemoryLRUCache(maxsize=maxsize)
                )
                self._region_caches[region] = region_cache
            elif isinstance(region_cache, _MemoryTLRUCache) != (self.cache_type == 'ttl'):
                raise ValueError(
                    f"Cache region {region!r} already uses a different cache type"
                )
            if isinstance(region_cache, _MemoryTLRUCache):
                region_cache.set(key, value, ttl=ttl)
            else:
                region_cache[key] = value

    def store(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **kwargs: Any,
    ) -> None:
        """
        严格写入内存缓存。
        """
        self.set(key=key, value=value, ttl=ttl, region=region, **kwargs)

    def consume(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Any:
        """
        在区域缓存锁内原子领取一个值。
        """
        with self._lock:
            region_cache = self.__get_region_cache(region or DEFAULT_CACHE_REGION)
            if region_cache is None:
                return None
            try:
                return region_cache.pop(key)
            except KeyError:
                return None

    def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在。

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return False
        with self._lock:
            return key in region_cache

    def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存的值。

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return None
        with self._lock:
            return region_cache.get(key)

    def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存。

        :param key: 缓存的键
        :param region: 缓存的区
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return
        with self._lock:
            if key in region_cache:
                del region_cache[key]

    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存。

        :param region: 缓存的区，为None时清空所有区缓存
        """
        with self._lock:
            if region:
                region_cache = self.__get_region_cache(region)
                if region_cache is not None:
                    region_cache.clear()
                    logger.debug(f"Cleared cache for region: {region}")
            else:
                for region_cache in self._region_caches.values():
                    region_cache.clear()
                logger.info("Cleared all cache")

    def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[Tuple[str, Any], None, None]:
        """
        获取指定区域的所有缓存项。

        :param region: 缓存的区
        :yields: 当前缓存区域中的键值对，每次产生一个二元组
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            yield from ()
            return
        # 使用锁保护迭代过程，避免在迭代时缓存被修改
        with self._lock:
            # 创建快照避免并发修改问题
            items_snapshot = list(region_cache.items())
        for item in items_snapshot:
            yield item

    def close(self) -> None:
        """
        内存缓存不需要关闭资源。
        """
        pass


class AsyncMemoryBackend(AsyncCacheBackend):
    """
    基于 `cachetools.TTLCache` 实现的异步缓存后端。

    :ivar _backend: 与同步调用共享区域、锁及存储的内存后端
    """

    def __init__(self, cache_type: Literal['ttl', 'lru'] = 'ttl',
                 maxsize: Optional[int] = None, ttl: Optional[int] = None):
        """
        初始化缓存实例。

        :param cache_type: 缓存类型，支持 'ttl'（默认）和 'lru'
        :param maxsize: 缓存的最大条目数
        :param ttl: 默认缓存存活时间，单位秒
        """
        self._backend = MemoryBackend(cache_type=cache_type, maxsize=maxsize, ttl=ttl)

    async def set(self, key: str, value: Any, ttl: Optional[int] = None,  # type: ignore[override]
                  region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs: object) -> None:
        """
        设置缓存值支持每个 key 独立配置 TTL。

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，未传入则使用 backend 默认值，单位秒
        :param region: 缓存的区
        """
        return self._backend.set(key=key, value=value, ttl=ttl, region=region, **kwargs)

    async def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:  # type: ignore[override]
        """
        判断缓存键是否存在。

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        return self._backend.exists(key=key, region=region)

    async def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存的值。

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        return self._backend.get(key=key, region=region)

    async def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:  # type: ignore[override]
        """
        删除缓存。

        :param key: 缓存的键
        :param region: 缓存的区
        """
        return self._backend.delete(key=key, region=region)

    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:  # type: ignore[override]
        """
        清除指定区域的缓存或全部缓存。

        :param region: 缓存的区，为None时清空所有区缓存
        """
        return self._backend.clear(region=region)

    async def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[Tuple[str, Any], None]:  # type: ignore[override]
        """
        获取指定区域的所有缓存项。

        :param region: 缓存的区
        :yields: 当前缓存区域中的键值对，每次产生一个二元组
        """
        for item in self._backend.items(region):
            yield item

    async def close(self) -> None:  # type: ignore[override]
        """
        内存缓存不需要关闭资源。
        """
        pass


@contextmanager
def fresh(fresh: bool = True) -> Generator[None, None, None]:
    """
    在当前调用链中临时跳过缓存读取，退出时恢复原刷新状态。

    嵌套上下文不会关闭外层已经开启的强制刷新；函数执行后的缓存写入仍遵守原策略。

    :param fresh: 是否开启强制刷新，默认为 True
    """
    token = _fresh.set(fresh or is_fresh())
    try:
        yield
    finally:
        _fresh.reset(token)


@asynccontextmanager
async def async_fresh(fresh: bool = True) -> AsyncGenerator[None, None]:
    """
    在当前异步调用链中临时跳过缓存读取，退出时恢复原刷新状态。

    嵌套上下文不会关闭外层已经开启的强制刷新；函数执行后的缓存写入仍遵守原策略。

    :param fresh: 是否开启强制刷新，默认为 True
    """
    token = _fresh.set(fresh or is_fresh())
    try:
        yield
    finally:
        _fresh.reset(token)


def is_fresh() -> bool:
    """
    判断当前上下文是否要求跳过缓存读取。

    :return: 当前调用链处于强制刷新上下文时返回 True
    """
    try:
        return _fresh.get()
    except LookupError:
        return False


def FileCache(
    base: Optional[Path] = None,
    ttl: Optional[int] = None,
    *,
    local_only: bool = False,
) -> CacheBackend:
    """
    获取文件缓存后端实例（Redis或文件系统），ttl仅在Redis环境中有效。

    默认路由保持历史契约：``CACHE_BACKEND_TYPE=redis`` 时返回 Redis 后端，
    供 Chain pickle 缓存、机器人登录态等小体积结构化状态在 Redis 中统一管理；
    插件通过 SDK 调用 ``FileCache()`` 的行为也因此保持不变。

    :param base: 文件系统缓存根目录，未传入时使用 ``TEMP_PATH``
    :param ttl: Redis 后端的默认存活时间，单位秒；文件系统后端不按键过期，
        由启动清理任务按目录与文件修改时间回收
    :param local_only: 为真时始终使用本地文件系统，忽略 Redis 配置。图片、种子、
        远程目录快照等大体积二进制载荷必须设置此项：写入 Redis 会占满其内存，
        在 ``allkeys-lru`` 策略下挤出共享实例中的其他数据，并让 RDB 快照反复
        重写整库造成持续磁盘写入
    """
    if not local_only and _backend_type_provider() == "redis":
        if _redis_factory is None:
            raise RuntimeError("Redis 缓存适配器尚未由启动层配置")
        return _redis_factory(ttl if ttl is not None else _file_ttl_provider())
    if _file_factory is None:
        raise RuntimeError("文件缓存适配器尚未由启动层配置")
    return _file_factory(base)


def AsyncFileCache(
    base: Optional[Path] = None,
    ttl: Optional[int] = None,
    *,
    local_only: bool = False,
) -> AsyncCacheBackend:
    """
    获取文件异步缓存后端实例（Redis或文件系统），ttl仅在Redis环境中有效。

    路由规则与 ``FileCache`` 一致；大体积二进制载荷须设置 ``local_only=True``，
    使同一缓存区的同步与异步实例始终落在同一存储上。

    :param base: 文件系统缓存根目录，未传入时使用 ``TEMP_PATH``
    :param ttl: Redis 后端的默认存活时间，单位秒
    :param local_only: 为真时始终使用本地文件系统，忽略 Redis 配置
    """
    if not local_only and _backend_type_provider() == "redis":
        if _async_redis_factory is None:
            raise RuntimeError("异步 Redis 缓存适配器尚未由启动层配置")
        return _async_redis_factory(
            ttl if ttl is not None else _file_ttl_provider()
        )
    if _async_file_factory is None:
        raise RuntimeError("异步文件缓存适配器尚未由启动层配置")
    return _async_file_factory(base)


def Cache(cache_type: Literal['ttl', 'lru'] = 'ttl',
          maxsize: Optional[int] = None,
          ttl: Optional[int] = None,
          *,
          local_only: bool = False) -> AtomicCacheBackend:
    """
    根据配置获取缓存后端实例（内存或Redis），maxsize仅在未启用Redis时生效。

    :param cache_type: 缓存类型，仅使用内存缓存时生效，支持 'ttl'（默认）和 'lru'
    :param maxsize: 缓存的最大条目数，仅使用cachetools时生效
    :param ttl: 缓存的默认存活时间，单位秒
    :param local_only: 为真时始终使用进程内有界内存缓存，忽略 Redis 配置；
        用于图片等二进制载荷，Redis 后端不支持 maxsize，写入后只能等 TTL 回收
    :return: 返回缓存后端实例
    """
    if not local_only and _backend_type_provider() == "redis":
        if _redis_factory is None:
            raise RuntimeError("Redis 缓存适配器尚未由启动层配置")
        return _redis_factory(ttl)
    return MemoryBackend(cache_type=cache_type, maxsize=maxsize, ttl=ttl)


def AsyncCache(cache_type: Literal['ttl', 'lru'] = 'ttl',
               maxsize: Optional[int] = None,
               ttl: Optional[int] = None,
               *,
               local_only: bool = False) -> AsyncCacheBackend:
    """
    根据配置获取异步缓存后端实例（内存或Redis），maxsize仅在未启用Redis时生效。

    :param cache_type: 缓存类型，仅使用内存缓存时生效，支持 'ttl'（默认）和 'lru'
    :param maxsize: 缓存的最大条目数，仅使用cachetools时生效
    :param ttl: 缓存的默认存活时间，单位秒
    :param local_only: 为真时始终使用进程内有界内存缓存，忽略 Redis 配置
    :return: 返回异步缓存后端实例
    """
    if not local_only and _backend_type_provider() == "redis":
        if _async_redis_factory is None:
            raise RuntimeError("异步 Redis 缓存适配器尚未由启动层配置")
        return _async_redis_factory(ttl)
    return AsyncMemoryBackend(cache_type=cache_type, maxsize=maxsize, ttl=ttl)


_CachedFunction = TypeVar("_CachedFunction", bound=Callable[..., Any])


class CacheControls(Protocol):
    """
    同步缓存包装器附加的管理接口，不改变原函数签名。

    :ivar cache_region: 包装函数使用的缓存区域
    :ivar cache_clear: 清空该函数的缓存区域
    :ivar cache_exists: 检查完整调用参数对应的缓存；实例方法需显式传入实例
    :ivar cache_delete: 删除完整调用参数对应的缓存；实例方法需显式传入实例
    """

    cache_region: str
    cache_clear: Callable[[], None]
    cache_exists: Callable[..., bool]
    cache_delete: Callable[..., None]


class AsyncCacheControls(Protocol):
    """
    异步缓存包装器附加的管理接口，各操作需要等待完成。

    :ivar cache_region: 包装函数使用的缓存区域
    :ivar cache_clear: 清空该函数的缓存区域
    :ivar cache_exists: 检查完整调用参数对应的缓存；实例方法需显式传入实例
    :ivar cache_delete: 删除完整调用参数对应的缓存；实例方法需显式传入实例
    """

    cache_region: str
    cache_clear: Callable[[], Coroutine[Any, Any, None]]
    cache_exists: Callable[..., Coroutine[Any, Any, bool]]
    cache_delete: Callable[..., Coroutine[Any, Any, None]]


def cached(region: Optional[str] = None, maxsize: Optional[int] = 1024, ttl: Optional[int] = None,
           ttl_provider: Optional[Callable[[], Optional[int]]] = None,
           skip_none: Optional[bool] = True, skip_empty: Optional[bool] = False, shared_key: Optional[str] = None,
           skip_if: Optional[Callable[[Any], bool]] = None,
           empty_ttl: Optional[int] = None, empty_if: Optional[Callable[[Any], bool]] = None,
           local_only: bool = False) -> Callable[[_CachedFunction], _CachedFunction]:
    """
    自定义缓存装饰器，支持配置缓存区域的 maxsize 和每个 key 的 ttl。

    :param region: 缓存区域的标识符，默认根据模块名、函数名等自动生成标识
    :param maxsize: 缓存区内的最大条目数
    :param ttl: 缓存的存活时间，单位秒；未传入时使用 LRU 缓存
    :param ttl_provider: 每次写入时解析 TTL 的配置快照工厂；用于可热更新配置，
        与固定 ttl 互斥
    :param skip_none: 跳过 None 缓存，默认为 True
    :param skip_empty: 跳过空值缓存（如 None, [], {}, "", set()），默认为 False
    :param shared_key: 同步/异步函数共享缓存的键，默认使用函数名（异步函数名会标准化为同步格式，如移除 `async_` 前缀）
    :param skip_if: 按返回值判断是否跳过缓存的谓词，返回真值时不缓存；用于
        「结构合法但业务失败」的返回值（如 TMDB 的 success=false 响应），
        这类值无法用 skip_none/skip_empty 表达
    :param empty_ttl: 空结果的独立存活时间，单位秒；判定为空的结果改用该 TTL 写入，
        使其比默认 ttl 更快过期（如故障期间产生的空响应），未传入时空结果沿用默认 ttl；
        仅在 ttl 模式下生效，LRU 模式无过期概念
    :param empty_if: 判断返回值是否为空结果的谓词，返回真值时按 empty_ttl 写入；
        未传入时按假值判断（None, [], {}, "", set() 等视为空）；用于「结构合法但
        内容为空」的返回值（如 TMDB 搜索结果快照中 results 为空列表）
    :param local_only: 为真时始终使用进程内有界内存缓存，忽略 Redis 配置；
        返回值包含图片等二进制内容时必须设置，否则 Redis 模式下 maxsize 失效
    :return: 装饰器函数
    """

    def decorator(func: _CachedFunction) -> _CachedFunction:
        """
        按函数签名创建保留同步或异步调用方式的缓存包装。
        """
        # 函数签名在装饰后不会变化，预计算可避免每次缓存访问都重复反射。
        signature = inspect.signature(func)
        parameter_names = list(signature.parameters.keys())
        cache_parameter_names = (
            parameter_names[1:]
            if parameter_names and parameter_names[0] in ("self", "cls")
            else parameter_names
        )

        def should_cache(value: Any) -> bool:
            """
            判断是否应该缓存结果，如果返回值是 None 或空值则不缓存。

            :param value: 要判断的缓存值
            :return: 是否缓存结果
            """
            if skip_none and value is None:
                return False
            if skip_empty and not value:
                return False
            if skip_if and skip_if(value):
                return False
            return True

        if ttl is not None and ttl_provider is not None:
            raise ValueError("cached 的 ttl 与 ttl_provider 不能同时设置")

        def get_cache_ttl(value: Any) -> Optional[int]:
            """
            返回写入该返回值时应使用的 TTL，空结果改用独立的短 TTL（empty_ttl）。

            :param value: 待写入缓存的返回值
            :return: 实际使用的 TTL，单位秒
            """
            configured_ttl = ttl_provider() if ttl_provider is not None else ttl
            if empty_ttl is None:
                return configured_ttl
            if value is None:
                return empty_ttl
            if empty_if is not None:
                return empty_ttl if empty_if(value) else configured_ttl
            return empty_ttl if not value else configured_ttl

        def is_valid_cache_value(_cache_key: str, _cached_value: Any, _cache_region: str) -> bool:
            """
            判断指定的值是否为一个有效的缓存值。

            :param _cache_key: 缓存的键
            :param _cached_value: 缓存的值
            :param _cache_region: 缓存的区
            :return: 若值是有效的缓存值返回 True，否则返回 False
            """
            # 如果 skip_none 为 False，且 value 为 None，需要判断缓存实际是否存在
            if not skip_none and _cached_value is None:
                if not sync_cache_backend.exists(key=_cache_key, region=_cache_region):
                    return False
            return True

        async def async_is_valid_cache_value(_cache_key: str, _cached_value: Any, _cache_region: str) -> bool:
            """
            判断指定的值是否为一个有效的缓存值（异步版本）。

            :param _cache_key: 缓存的键
            :param _cached_value: 缓存的值
            :param _cache_region: 缓存的区
            :return: 若值是有效的缓存值返回 True，否则返回 False
            """
            # 如果 skip_none 为 False，且 value 为 None，需要判断缓存实际是否存在
            if not skip_none and _cached_value is None:
                if not await async_cache_backend.exists(key=_cache_key, region=_cache_region):
                    return False
            return True

        def __standardize_func_name() -> str:
            """
            将异步函数名标准化为同步函数的命名，以生成统一的缓存键。
            """
            # 同步与异步函数默认按 async_ 前缀或 _async 后缀配对；其他命名必须通过 shared_key 显式指定。
            return (
                func.__name__.removeprefix("async_").removesuffix("_async")
                if is_async
                else func.__name__
            )

        def __get_cache_key(args: tuple[object, ...], kwargs: dict[str, object]) -> str:
            """
            根据函数和参数生成缓存键。

            :param args: 位置参数
            :param kwargs: 关键字参数
            :return: 缓存键
            """
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            keys = [
                bound.arguments[param] for param in cache_parameter_names if param in bound.arguments
            ]
            return f"{func_name}_{hashkey(*keys)}"

        # 被装饰函数的上层名称（如类名或外层函数名）
        enclosing_name = (
            func.__qualname__[:last_dot]
            if (last_dot := func.__qualname__.rfind(".")) != -1
            else ""
        )
        is_async = inspect.iscoroutinefunction(func)
        # 生成标准化后的函数名称，用于同步/异步函数共享缓存
        func_name = shared_key if shared_key else __standardize_func_name()
        cache_region = (
            region if region is not None else f"{func.__module__}:{enclosing_name}:{func_name}"
        )

        if is_async:
            async_cache_backend = AsyncCache(
                cache_type="ttl" if ttl is not None or ttl_provider is not None else "lru",
                maxsize=maxsize,
                ttl=ttl if ttl is not None else 1,
                local_only=local_only,
            )

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                """
                异步读取有效缓存；强制刷新或未命中时等待原函数并按策略写入结果。
                """
                cache_key = __get_cache_key(args, kwargs)

                if not is_fresh():
                    cached_value = await async_cache_backend.get(cache_key, region=cache_region)
                    if should_cache(cached_value) and await async_is_valid_cache_value(cache_key, cached_value,
                                                                                       cache_region):
                        return cached_value
                result = await func(*args, **kwargs)
                if not should_cache(result):
                    return result
                # 设置缓存（如果有传入的 maxsize 和 ttl，则覆盖默认值；空结果使用独立的短 TTL）
                await async_cache_backend.set(cache_key, result, ttl=get_cache_ttl(result), maxsize=maxsize,
                                        region=cache_region)
                return result

            async def async_cache_clear() -> None:
                """
                清理缓存区。
                """
                await async_cache_backend.clear(region=cache_region)

            async def async_cache_exists(*args: object, **kwargs: object) -> bool:
                """
                判断当前参数对应的有效缓存是否存在。
                """
                cache_key = __get_cache_key(args, kwargs)
                cached_value = await async_cache_backend.get(cache_key, region=cache_region)
                return should_cache(cached_value) and await async_is_valid_cache_value(
                    cache_key, cached_value, cache_region
                )

            async def async_cache_delete(*args: object, **kwargs: object) -> None:
                """
                删除当前参数对应的缓存。
                """
                cache_key = __get_cache_key(args, kwargs)
                await async_cache_backend.delete(cache_key, region=cache_region)

            async_cached = cast(AsyncCacheControls, async_wrapper)
            async_cached.cache_region = cache_region
            async_cached.cache_clear = async_cache_clear
            async_cached.cache_exists = async_cache_exists
            async_cached.cache_delete = async_cache_delete
            return cast(_CachedFunction, async_wrapper)
        else:
            sync_cache_backend = Cache(
                cache_type="ttl" if ttl is not None or ttl_provider is not None else "lru",
                maxsize=maxsize,
                ttl=ttl if ttl is not None else 1,
                local_only=local_only,
            )

            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                """
                优先返回有效缓存；强制刷新或未命中时执行同步函数并按策略写入结果。
                """
                cache_key = __get_cache_key(args, kwargs)

                if not is_fresh():
                    cached_value = sync_cache_backend.get(cache_key, region=cache_region)
                    if should_cache(cached_value) and is_valid_cache_value(cache_key, cached_value, cache_region):
                        return cached_value
                result = func(*args, **kwargs)
                if not should_cache(result):
                    return result
                # 设置缓存（如果有传入的 maxsize 和 ttl，则覆盖默认值；空结果使用独立的短 TTL）
                sync_cache_backend.set(cache_key, result, ttl=get_cache_ttl(result), maxsize=maxsize, region=cache_region)
                return result

            def cache_clear() -> None:
                """
                清理缓存区。
                """
                sync_cache_backend.clear(region=cache_region)

            def cache_exists(*args: object, **kwargs: object) -> bool:
                """
                判断当前参数对应的有效缓存是否存在。
                """
                cache_key = __get_cache_key(args, kwargs)
                cached_value = sync_cache_backend.get(cache_key, region=cache_region)
                return should_cache(cached_value) and is_valid_cache_value(
                    cache_key, cached_value, cache_region
                )

            def cache_delete(*args: object, **kwargs: object) -> None:
                """
                删除当前参数对应的缓存。
                """
                cache_key = __get_cache_key(args, kwargs)
                sync_cache_backend.delete(cache_key, region=cache_region)

            sync_cached = cast(CacheControls, wrapper)
            sync_cached.cache_region = cache_region
            sync_cached.cache_clear = cache_clear
            sync_cached.cache_exists = cache_exists
            sync_cached.cache_delete = cache_delete
            return cast(_CachedFunction, wrapper)

    return decorator


class CacheProxy:
    """
    缓存代理类，将缓存后端的方法直接代理到实例上。

    :ivar _cache_backend: 实际执行读写与原子领取操作的同步后端
    :ivar _region: 未传入区域选项时绑定的区域
    """

    def __init__(self, cache_backend: AtomicCacheBackend, region: Optional[str]):
        """
        初始化缓存代理。

        :param cache_backend: 缓存后端实例
        :param region: 缓存区域
        """
        self._cache_backend = cache_backend
        self._region = region

    def __getitem__(self, key: str) -> Any:
        """
        按字典下标读取绑定区域的缓存值。

        :param key: 绑定缓存区域内的条目标识
        :return: 当前缓存值
        :raises KeyError: 后端返回 None，包括未命中或缓存值本身为 None
        """
        value = self._cache_backend.get(key, region=self._region)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        """
        设置缓存项。
        """
        self._cache_backend.set(key, value, region=self._region)

    def __delitem__(self, key: str) -> None:
        """
        按字典语义删除绑定区域中的缓存键。

        :param key: 绑定缓存区域内的条目标识
        :raises KeyError: 后端确认指定键不存在
        """
        if not self._cache_backend.exists(key, region=self._region):
            raise KeyError(key)
        self._cache_backend.delete(key, region=self._region)

    def __contains__(self, key: str) -> bool:
        """
        检查键是否存在。
        """
        return self._cache_backend.exists(key, region=self._region)

    def __iter__(self) -> Generator[str, None, None]:
        """
        返回缓存的迭代器。
        """
        for key, _ in self._cache_backend.items(region=self._region):
            yield key

    def __len__(self) -> int:
        """
        返回缓存项的数量。
        """
        return sum(1 for _ in self._cache_backend.items(region=self._region))

    def is_redis(self) -> bool:
        """
        检查当前缓存后端是否为 Redis。
        """
        return self._cache_backend.is_redis()

    def get(self, key: str, **kwargs: Unpack[_RegionOptions]) -> Any:
        """
        从选定缓存区域读取指定键。

        :param key: 当前缓存区域内的条目标识
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 缓存值；未命中时返回后端约定的空值
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.get(key, **kwargs)

    def set(self, key: str, value: Any, **kwargs: Unpack[_WriteOptions]) -> None:
        """
        向选定缓存区域写入一个键值。

        :param key: 当前缓存区域内的条目标识
        :param value: 要缓存的业务值
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.set(key, value, **kwargs)

    def store(self, key: str, value: Any, **kwargs: Unpack[_WriteOptions]) -> None:
        """
        严格写入缓存，后端故障向调用方传播。
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.store(key, value, **kwargs)

    def consume(self, key: str, **kwargs: Unpack[_RegionOptions]) -> Any:
        """
        原子领取并删除缓存值。
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.consume(key, **kwargs)

    def delete(self, key: str, **kwargs: Unpack[_RegionOptions]) -> None:
        """
        从选定缓存区域删除指定键。

        :param key: 当前缓存区域内的条目标识
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.delete(key, **kwargs)

    def exists(self, key: str, **kwargs: Unpack[_RegionOptions]) -> bool:
        """
        检查选定缓存区域中是否存在指定键。

        :param key: 当前缓存区域内的条目标识
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 指定键是否存在
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.exists(key, **kwargs)

    def clear(self, **kwargs: Unpack[_RegionOptions]) -> None:
        """
        清除选定缓存区域中的全部条目。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.clear(**kwargs)

    def items(self, **kwargs: Unpack[_RegionOptions]) -> Generator[Tuple[str, Any], None, None]:
        """
        遍历选定缓存区域中的键值对。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 缓存中的键值对
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.items(**kwargs)

    def keys(self, **kwargs: Unpack[_RegionOptions]) -> Generator[str, None, None]:
        """
        遍历选定缓存区域中的键。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 缓存中的键
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.keys(**kwargs)

    def values(self, **kwargs: Unpack[_RegionOptions]) -> Generator[Any, None, None]:
        """
        遍历选定缓存区域中的值。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 缓存中的值
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.values(**kwargs)

    def update(self, other: Dict[str, Any], **kwargs: Unpack[_WriteOptions]) -> None:
        """
        将给定映射写入选定缓存区域。

        :param other: 需要写入的键值映射
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.update(other, **kwargs)

    def pop(self, key: str, default: Any = None, **kwargs: Unpack[_RegionOptions]) -> Any:
        """
        读取并删除指定键，未命中时按后端规则处理默认值。

        :param key: 当前缓存区域内的条目标识
        :param default: 缓存未命中时使用的值；默认 None 不表示缓存未命中时必定返回 None
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 被移除的缓存值；未命中且提供非 None 默认值时返回该默认值
        :raises KeyError: 默认后端未命中且 default 为 None
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.pop(key, default, **kwargs)

    def popitem(self, **kwargs: Unpack[_RegionOptions]) -> Tuple[str, Any]:
        """
        由后端选择并移除一个缓存条目。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 后端选中的键值对，条目顺序和空缓存行为由后端定义
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.popitem(**kwargs)

    def setdefault(self, key: str, default: Any = None, **kwargs: Unpack[_WriteOptions]) -> Any:
        """
        读取指定键，未命中时写入并返回默认值。

        :param key: 当前缓存区域内的条目标识
        :param default: 缓存未命中时使用的值，默认为 None
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 原缓存值或本次写入的默认值
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.setdefault(key, default, **kwargs)

    def close(self) -> None:
        """
        释放此代理使用的后端连接，具体关闭行为由后端实现。
        """
        self._cache_backend.close()


class AsyncCacheProxy:
    """
    异步缓存代理类，将异步缓存后端的方法直接代理到实例上。

    与同步 CacheProxy 的唯一差异是方法均为 async，默认绑定构造时指定的 region。

    :ivar _cache_backend: 实际执行异步读写的缓存后端
    :ivar _region: 未传入区域选项时绑定的区域
    """

    def __init__(self, cache_backend: AsyncCacheBackend, region: Optional[str]):
        """
        初始化异步缓存代理。

        :param cache_backend: 异步缓存后端实例
        :param region: 缓存区域
        """
        self._cache_backend = cache_backend
        self._region = region

    def is_redis(self) -> bool:
        """
        检查当前缓存后端是否为 Redis（纯状态判断，无需 await）。
        """
        return self._cache_backend.is_redis()

    async def get(self, key: str, **kwargs: Unpack[_RegionOptions]) -> Any:
        """
        从选定缓存区域读取指定键。

        :param key: 当前缓存区域内的条目标识
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 缓存值；未命中时返回后端约定的空值
        """
        kwargs.setdefault('region', self._region)
        return await self._cache_backend.get(key, **kwargs)

    async def set(self, key: str, value: Any, **kwargs: Unpack[_WriteOptions]) -> None:
        """
        向选定缓存区域写入一个键值。

        :param key: 当前缓存区域内的条目标识
        :param value: 要缓存的业务值
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        """
        kwargs.setdefault('region', self._region)
        await self._cache_backend.set(key, value, **kwargs)

    async def delete(self, key: str, **kwargs: Unpack[_RegionOptions]) -> None:
        """
        从选定缓存区域删除指定键。

        :param key: 当前缓存区域内的条目标识
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        """
        kwargs.setdefault('region', self._region)
        await self._cache_backend.delete(key, **kwargs)

    async def exists(self, key: str, **kwargs: Unpack[_RegionOptions]) -> bool:
        """
        检查选定缓存区域中是否存在指定键。

        :param key: 当前缓存区域内的条目标识
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 指定键是否存在
        """
        kwargs.setdefault('region', self._region)
        return await self._cache_backend.exists(key, **kwargs)

    async def clear(self, **kwargs: Unpack[_RegionOptions]) -> None:
        """
        清除选定缓存区域中的全部条目。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        """
        kwargs.setdefault('region', self._region)
        await self._cache_backend.clear(**kwargs)

    async def items(self, **kwargs: Unpack[_RegionOptions]) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        遍历选定缓存区域中的键值对。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :yields: 缓存中的键值对
        """
        kwargs.setdefault('region', self._region)
        async for item in self._cache_backend.items(**kwargs):
            yield item

    async def keys(self, **kwargs: Unpack[_RegionOptions]) -> AsyncGenerator[str, None]:
        """
        遍历选定缓存区域中的键。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :yields: 缓存中的键
        """
        kwargs.setdefault('region', self._region)
        async for key in self._cache_backend.keys(**kwargs):
            yield key

    async def values(self, **kwargs: Unpack[_RegionOptions]) -> AsyncGenerator[Any, None]:
        """
        遍历选定缓存区域中的值。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :yields: 缓存中的值
        """
        kwargs.setdefault('region', self._region)
        async for value in self._cache_backend.values(**kwargs):
            yield value

    async def update(self, other: Dict[str, Any], **kwargs: Unpack[_WriteOptions]) -> None:
        """
        将给定映射写入选定缓存区域。

        :param other: 需要写入的键值映射
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        """
        kwargs.setdefault('region', self._region)
        await self._cache_backend.update(other, **kwargs)

    async def pop(self, key: str, default: Any = None, **kwargs: Unpack[_RegionOptions]) -> Any:
        """
        读取并删除指定键，未命中时按后端规则处理默认值。

        :param key: 当前缓存区域内的条目标识
        :param default: 缓存未命中时使用的值；默认 None 不表示缓存未命中时必定返回 None
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 被移除的缓存值；未命中且提供非 None 默认值时返回该默认值
        :raises KeyError: 默认后端未命中且 default 为 None
        """
        kwargs.setdefault('region', self._region)
        return await self._cache_backend.pop(key, default, **kwargs)

    async def popitem(self, **kwargs: Unpack[_RegionOptions]) -> Tuple[str, Any]:
        """
        由后端选择并移除一个缓存条目。

        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 后端选中的键值对，条目顺序和空缓存行为由后端定义
        """
        kwargs.setdefault('region', self._region)
        return await self._cache_backend.popitem(**kwargs)

    async def setdefault(self, key: str, default: Any = None, **kwargs: Unpack[_WriteOptions]) -> Any:
        """
        读取指定键，未命中时写入并返回默认值。

        :param key: 当前缓存区域内的条目标识
        :param default: 缓存未命中时使用的值，默认为 None
        :param kwargs: 透传给后端的选项，未传 region 时使用构造时绑定的区域
        :return: 原缓存值或本次写入的默认值
        """
        kwargs.setdefault('region', self._region)
        return await self._cache_backend.setdefault(key, default, **kwargs)

    async def close(self) -> None:
        """
        释放此代理使用的后端连接，具体关闭行为由后端实现。
        """
        await self._cache_backend.close()


class AsyncTTLCache(AsyncCacheProxy):
    """
    基于 TTL 的异步缓存类，与同步 TTLCache 使用同一 region 语义，
    内存后端共享进程内存储，Redis 后端共享同一键空间
    """

    def __init__(self,
                 region: Optional[str] = DEFAULT_CACHE_REGION,
                 maxsize: Optional[int] = DEFAULT_CACHE_SIZE,
                 ttl: Optional[int] = DEFAULT_CACHE_TTL):
        """
        初始化异步 TTL 缓存。

        :param maxsize: 缓存的最大条目数
        :param ttl: 缓存的存活时间，单位秒
        :param region: 缓存的区，为 None 时使用默认区
        """
        super().__init__(AsyncCache(cache_type='ttl', maxsize=maxsize, ttl=ttl), region)


class TTLCache(CacheProxy):
    """
    基于 TTL 的缓存类，兼容 cachetools.TTLCache 接口
    使用项目的缓存后端实现，支持 Redis 和内存缓存
    """

    def __init__(self,
                 region: Optional[str] = DEFAULT_CACHE_REGION,
                 maxsize: Optional[int] = DEFAULT_CACHE_SIZE,
                 ttl: Optional[int] = DEFAULT_CACHE_TTL):
        """
        初始化 TTL 缓存。

        :param maxsize: 缓存的最大条目数
        :param ttl: 缓存的存活时间，单位秒
        :param region: 缓存的区，为 None 时使用默认区
        """
        super().__init__(Cache(cache_type='ttl', maxsize=maxsize, ttl=ttl), region)


class LRUCache(CacheProxy):
    """
    基于 LRU 的缓存类，兼容 cachetools.LRUCache 接口
    使用项目的缓存后端实现，支持 Redis 和内存缓存
    """

    def __init__(self,
                 region: Optional[str] = DEFAULT_CACHE_REGION,
                 maxsize: Optional[int] = DEFAULT_CACHE_SIZE
                 ):
        """
        初始化 LRU 缓存。

        :param maxsize: 缓存的最大条目数
        :param region: 缓存的区，为 None 时使用默认区
        """
        super().__init__(Cache(cache_type='lru', maxsize=maxsize), region)
