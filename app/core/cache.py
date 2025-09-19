import inspect
import shutil
import tempfile
import threading
from abc import ABC, abstractmethod
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional, Generator, AsyncGenerator, Tuple, Literal, Union

import aiofiles
import aioshutil
from anyio import Path as AsyncPath
from cachetools import LRUCache as MemoryLRUCache
from cachetools import TTLCache as MemoryTTLCache
from cachetools.keys import hashkey

from app.core.config import settings
from app.helper.redis import RedisHelper, AsyncRedisHelper
from app.log import logger

# 默认缓存区
DEFAULT_CACHE_REGION = "DEFAULT"
# 默认缓存大小
DEFAULT_CACHE_SIZE = 1024
# 默认缓存有效期
DEFAULT_CACHE_TTL = 365 * 24 * 60 * 60

lock = threading.Lock()


class CacheBackend(ABC):
    """
    缓存后端基类，定义通用的缓存接口
    """

    def __getitem__(self, key: str) -> Any:
        """
        获取缓存项，类似 dict[key]
        """
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        """
        设置缓存项，类似 dict[key] = value
        """
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        """
        删除缓存项，类似 del dict[key]
        """
        if not self.exists(key):
            raise KeyError(key)
        self.delete(key)

    def __contains__(self, key: str) -> bool:
        """
        检查键是否存在，类似 key in dict
        """
        return self.exists(key)

    def __iter__(self):
        """
        返回缓存的迭代器，类似 iter(dict)
        """
        for key, _ in self.items():
            yield key

    def __len__(self) -> int:
        """
        返回缓存项的数量，类似 len(dict)
        """
        return sum(1 for _ in self.items())

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存

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
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        pass

    @abstractmethod
    def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        pass

    @abstractmethod
    def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        pass

    @abstractmethod
    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区，为None时清空所有区缓存
        """
        pass

    @abstractmethod
    def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[Tuple[str, Any], None, None]:
        """
        获取指定区域的所有缓存项

        :param region: 缓存的区
        :return: 返回一个字典，包含所有缓存键值对
        """
        pass

    def keys(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[str, None, None]:
        """
        获取所有缓存键，类似 dict.keys()
        """
        for key, _ in self.items(region=region):
            yield key

    def values(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[Any, None, None]:
        """
        获取所有缓存值，类似 dict.values()
        """
        for _, value in self.items(region=region):
            yield value

    def update(self, other: Dict[str, Any], region: Optional[str] = DEFAULT_CACHE_REGION,
               ttl: Optional[int] = None, **kwargs) -> None:
        """
        更新缓存，类似 dict.update()
        """
        for key, value in other.items():
            self.set(key, value, ttl=ttl, region=region, **kwargs)

    def pop(self, key: str, default: Any = None, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        弹出缓存项，类似 dict.pop()
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
        弹出最后一个缓存项，类似 dict.popitem()
        """
        items = list(self.items(region=region))
        if not items:
            raise KeyError("popitem(): cache is empty")
        key, value = items[-1]
        self.delete(key, region=region)
        return key, value

    def setdefault(self, key: str, default: Any = None, region: Optional[str] = DEFAULT_CACHE_REGION,
                   ttl: Optional[int] = None, **kwargs) -> Any:
        """
        设置默认值，类似 dict.setdefault()
        """
        value = self.get(key, region=region)
        if value is None:
            self.set(key, default, ttl=ttl, region=region, **kwargs)
            return default
        return value

    @abstractmethod
    def close(self) -> None:
        """
        关闭缓存连接
        """
        pass

    @staticmethod
    def get_region(region: Optional[str] = None) -> str:
        """
        获取缓存的区
        """
        return f"region:{region}" if region else "region:default"

    @staticmethod
    def is_redis() -> bool:
        """
        判断当前缓存后端是否为 Redis
        """
        return settings.CACHE_BACKEND_TYPE == "redis"


class AsyncCacheBackend(CacheBackend):
    """
    缓存后端基类，定义通用的缓存接口（异步）
    """

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: Optional[int] = None,
                  region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，单位秒
        :param region: 缓存的区
        :param kwargs: 其他参数
        """
        pass

    @abstractmethod
    async def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        pass

    @abstractmethod
    async def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        pass

    @abstractmethod
    async def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        pass

    @abstractmethod
    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区，为None时清空所有区缓存
        """
        pass

    @abstractmethod
    async def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        获取指定区域的所有缓存项

        :param region: 缓存的区
        :return: 返回一个字典，包含所有缓存键值对
        """
        pass

    async def keys(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[str, None]:
        """
        获取所有缓存键，类似 dict.keys()（异步）
        """
        async for key, _ in await self.items(region=region):
            yield key

    async def values(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[Any, None]:
        """
        获取所有缓存值，类似 dict.values()（异步）
        """
        async for _, value in await self.items(region=region):
            yield value

    async def update(self, other: Dict[str, Any], region: Optional[str] = DEFAULT_CACHE_REGION,
                     ttl: Optional[int] = None, **kwargs) -> None:
        """
        更新缓存，类似 dict.update()（异步）
        """
        for key, value in other.items():
            await self.set(key, value, ttl=ttl, region=region, **kwargs)

    async def pop(self, key: str, default: Any = None, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        弹出缓存项，类似 dict.pop()（异步）
        """
        value = await self.get(key, region=region)
        if value is not None:
            await self.delete(key, region=region)
            return value
        if default is not None:
            return default
        raise KeyError(key)

    async def popitem(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Tuple[str, Any]:
        """
        弹出最后一个缓存项，类似 dict.popitem()（异步）
        """
        items = []
        async for item in await self.items(region=region):
            items.append(item)
        if not items:
            raise KeyError("popitem(): cache is empty")
        key, value = items[-1]
        await self.delete(key, region=region)
        return key, value

    async def setdefault(self, key: str, default: Any = None, region: Optional[str] = DEFAULT_CACHE_REGION,
                         ttl: Optional[int] = None, **kwargs) -> Any:
        """
        设置默认值，类似 dict.setdefault()（异步）
        """
        value = await self.get(key, region=region)
        if value is None:
            await self.set(key, default, ttl=ttl, region=region, **kwargs)
            return default
        return value

    @abstractmethod
    async def close(self) -> None:
        """
        关闭缓存连接
        """
        pass


class MemoryBackend(CacheBackend):
    """
    基于 `cachetools.TTLCache` 实现的缓存后端
    """

    def __init__(self, cache_type: Literal['ttl', 'lru'] = 'ttl',
                 maxsize: Optional[int] = None, ttl: Optional[int] = None):
        """
        初始化缓存实例

        :param cache_type: 缓存类型，支持 'ttl'（默认）和 'lru'
        :param maxsize: 缓存的最大条目数
        :param ttl: 默认缓存存活时间，单位秒
        """
        self.cache_type = cache_type
        self.maxsize = maxsize or DEFAULT_CACHE_SIZE
        self.ttl = ttl or DEFAULT_CACHE_TTL
        # 存储各个 region 的缓存实例，region -> TTLCache
        self._region_caches: Dict[str, Union[MemoryTTLCache, MemoryLRUCache]] = {}

    def __get_region_cache(self, region: str) -> Optional[Union[MemoryTTLCache, MemoryLRUCache]]:
        """
        获取指定区域的缓存实例，如果不存在则返回 None
        """
        region = self.get_region(region)
        return self._region_caches.get(region)

    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存值支持每个 key 独立配置 TTL

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，不传入为永久缓存，单位秒
        :param region: 缓存的区
        """
        ttl = ttl or self.ttl
        maxsize = kwargs.get("maxsize", self.maxsize)
        region = self.get_region(region)
        # 设置缓存值
        with lock:
            # 如果该 key 尚未有缓存实例，则创建一个新的 TTLCache 实例
            region_cache = self._region_caches.setdefault(
                region,
                MemoryTTLCache(maxsize=maxsize, ttl=ttl) if self.cache_type == 'ttl'
                else MemoryLRUCache(maxsize=maxsize)
            )
            region_cache[key] = value

    def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return False
        return key in region_cache

    def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return None
        return region_cache.get(key)

    def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION):
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return
        with lock:
            del region_cache[key]

    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区，为None时清空所有区缓存
        """
        if region:
            # 清理指定缓存区
            region_cache = self.__get_region_cache(region)
            if region_cache:
                with lock:
                    region_cache.clear()
                logger.debug(f"Cleared cache for region: {region}")
        else:
            # 清除所有区域的缓存
            for region_cache in self._region_caches.values():
                with lock:
                    region_cache.clear()
            logger.info("Cleared all cache")

    def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[Tuple[str, Any], None, None]:
        """
        获取指定区域的所有缓存项

        :param region: 缓存的区
        :return: 返回一个字典，包含所有缓存键值对
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            yield from ()
            return
        # 使用锁保护迭代过程，避免在迭代时缓存被修改
        with lock:
            # 创建快照避免并发修改问题
            items_snapshot = list(region_cache.items())
        for item in items_snapshot:
            yield item

    def close(self) -> None:
        """
        内存缓存不需要关闭资源
        """
        pass


class AsyncMemoryBackend(AsyncCacheBackend):
    """
    基于 `cachetools.TTLCache` 实现的异步缓存后端
    """

    def __init__(self, cache_type: Literal['ttl', 'lru'] = 'ttl',
                 maxsize: Optional[int] = None, ttl: Optional[int] = None):
        """
        初始化缓存实例

        :param cache_type: 缓存类型，支持 'ttl'（默认）和 'lru'
        :param maxsize: 缓存的最大条目数
        :param ttl: 默认缓存存活时间，单位秒
        """
        self.cache_type = cache_type
        self.maxsize = maxsize or DEFAULT_CACHE_SIZE
        self.ttl = ttl or DEFAULT_CACHE_TTL
        # 存储各个 region 的缓存实例，region -> TTLCache
        self._region_caches: Dict[str, Union[MemoryTTLCache, MemoryLRUCache]] = {}

    def __get_region_cache(self, region: str) -> Optional[Union[MemoryTTLCache, MemoryLRUCache]]:
        """
        获取指定区域的缓存实例，如果不存在则返回 None
        """
        region = self.get_region(region)
        return self._region_caches.get(region)

    async def set(self, key: str, value: Any, ttl: Optional[int] = None,
                  region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存值支持每个 key 独立配置 TTL

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，不传入为永久缓存，单位秒
        :param region: 缓存的区
        """
        ttl = ttl or self.ttl
        maxsize = kwargs.get("maxsize", self.maxsize)
        region = self.get_region(region)
        # 设置缓存值
        with lock:
            # 如果该 key 尚未有缓存实例，则创建一个新的 TTLCache 实例
            region_cache = self._region_caches.setdefault(
                region,
                MemoryTTLCache(maxsize=maxsize, ttl=ttl) if self.cache_type == 'ttl'
                else MemoryLRUCache(maxsize=maxsize)
            )
            region_cache[key] = value

    async def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return False
        return key in region_cache

    async def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Any:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return None
        return region_cache.get(key)

    async def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION):
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return
        with lock:
            del region_cache[key]

    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区，为None时清空所有区缓存
        """
        if region:
            # 清理指定缓存区
            region_cache = self.__get_region_cache(region)
            if region_cache:
                with lock:
                    region_cache.clear()
                logger.debug(f"Cleared cache for region: {region}")
        else:
            # 清除所有区域的缓存
            for region_cache in self._region_caches.values():
                with lock:
                    region_cache.clear()
            logger.info("All cache cleared！")

    async def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        获取指定区域的所有缓存项

        :param region: 缓存的区
        :return: 返回一个字典，包含所有缓存键值对
        """
        region_cache = self.__get_region_cache(region)
        if region_cache is None:
            return
        # 使用锁保护迭代过程，避免在迭代时缓存被修改
        with lock:
            # 创建快照避免并发修改问题
            items_snapshot = list(region_cache.items())
        for item in items_snapshot:
            yield item

    async def close(self) -> None:
        """
        内存缓存不需要关闭资源
        """
        pass


class RedisBackend(CacheBackend):
    """
    基于 Redis 实现的缓存后端，支持通过 Redis 存储缓存
    """

    def __init__(self, ttl: Optional[int] = None):
        """
        初始化 Redis 缓存实例

        :param ttl: 缓存的存活时间，单位秒
        """
        self.ttl = ttl
        self.redis_helper = RedisHelper()

    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，未传入则为永久缓存，单位秒
        :param region: 缓存的区
        :param kwargs: kwargs
        """
        ttl = ttl or self.ttl
        self.redis_helper.set(key, value, ttl=ttl, region=region, **kwargs)

    def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        return self.redis_helper.exists(key, region=region)

    def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Optional[Any]:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        return self.redis_helper.get(key, region=region)

    def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        self.redis_helper.delete(key, region=region)

    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区，为None时清空所有区缓存
        """
        self.redis_helper.clear(region=region)

    def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[Tuple[str, Any], None, None]:
        """
        获取指定区域的所有缓存项

        :param region: 缓存的区
        :return: 返回一个字典，包含所有缓存键值对
        """
        return self.redis_helper.items(region=region)

    def close(self) -> None:
        """
        关闭 Redis 客户端的连接池
        """
        self.redis_helper.close()


class AsyncRedisBackend(AsyncCacheBackend):
    """
    基于 Redis 实现的缓存后端，支持通过 Redis 存储缓存
    """

    def __init__(self, ttl: Optional[int] = None):
        """
        初始化 Redis 缓存实例

        :param ttl: 缓存的存活时间，单位秒
        """
        self.ttl = ttl
        self.redis_helper = AsyncRedisHelper()

    async def set(self, key: str, value: Any, ttl: Optional[int] = None,
                  region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，未传入则为永久缓存，单位秒
        :param region: 缓存的区
        :param kwargs: kwargs
        """
        ttl = ttl or self.ttl
        await self.redis_helper.set(key, value, ttl=ttl, region=region, **kwargs)

    async def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        return await self.redis_helper.exists(key, region=region)

    async def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Optional[Any]:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        return await self.redis_helper.get(key, region=region)

    async def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        await self.redis_helper.delete(key, region=region)

    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区，为None时清空所有区缓存
        """
        await self.redis_helper.clear(region=region)

    async def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        获取指定区域的所有缓存项

        :param region: 缓存的区
        :return: 返回一个字典，包含所有缓存键值对
        """
        async for item in self.redis_helper.items(region=region):
            yield item

    async def close(self) -> None:
        """
        关闭 Redis 客户端的连接池
        """
        await self.redis_helper.close()


class FileBackend(CacheBackend):
    """
    基于 文件系统 实现的缓存后端
    """

    def __init__(self, base: Path):
        """
        初始化文件缓存实例
        """
        self.base = base
        if not self.base.exists():
            self.base.mkdir(parents=True, exist_ok=True)

    def set(self, key: str, value: Any, region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param region: 缓存的区
        :param kwargs: kwargs
        """
        cache_path = self.base / region / key
        # 确保缓存目录存在
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 将值序列化为字符串存储
        with tempfile.NamedTemporaryFile(dir=cache_path.parent, delete=False) as tmp_file:
            tmp_file.write(value)
            temp_path = Path(tmp_file.name)
        temp_path.replace(cache_path)

    def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        cache_path = self.base / region / key
        return cache_path.exists()

    def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Optional[Any]:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        cache_path = self.base / region / key
        if not cache_path.exists():
            return None
        with open(cache_path, 'rb') as f:
            return f.read()

    def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        cache_path = self.base / region / key
        if cache_path.exists():
            cache_path.unlink()

    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区，为None时清空所有区缓存
        """
        if region:
            # 清理指定缓存区
            cache_path = self.base / region
            if cache_path.exists():
                for item in cache_path.iterdir():
                    if item.is_file():
                        item.unlink()
                    else:
                        shutil.rmtree(item, ignore_errors=True)
        else:
            # 清除所有区域的缓存
            for item in self.base.iterdir():
                if item.is_file():
                    item.unlink()
                else:
                    shutil.rmtree(item, ignore_errors=True)

    def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> Generator[Tuple[str, Any], None, None]:
        """
        获取指定区域的所有缓存项

        :param region: 缓存的区
        :return: 返回一个字典，包含所有缓存键值对
        """
        cache_path = self.base / region
        if not cache_path.exists():
            yield from ()
            return
        for item in cache_path.iterdir():
            if item.is_file():
                with open(item, 'r') as f:
                    yield item.as_posix(), f.read()

    def close(self) -> None:
        """
        关闭 Redis 客户端的连接池
        """
        pass


class AsyncFileBackend(AsyncCacheBackend):
    """
    基于 文件系统 实现的缓存后端（异步模式）
    """

    def __init__(self, base: Path):
        """
        初始化文件缓存实例
        """
        self.base = base
        if not self.base.exists():
            self.base.mkdir(parents=True, exist_ok=True)

    async def set(self, key: str, value: Any, region: Optional[str] = DEFAULT_CACHE_REGION, **kwargs) -> None:
        """
        设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param region: 缓存的区
        :param kwargs: kwargs
        """
        cache_path = AsyncPath(self.base) / region / key
        # 确保缓存目录存在
        await cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 保存文件
        async with aiofiles.tempfile.NamedTemporaryFile(dir=cache_path.parent, delete=False) as tmp_file:
            await tmp_file.write(value)
            temp_path = AsyncPath(tmp_file.name)
        await temp_path.replace(cache_path)

    async def exists(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> bool:
        """
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回 True，否则返回 False
        """
        cache_path = AsyncPath(self.base) / region / key
        return await cache_path.exists()

    async def get(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> Optional[Any]:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回 None
        """
        cache_path = AsyncPath(self.base) / region / key
        if not await cache_path.exists():
            return None
        async with aiofiles.open(cache_path, 'rb') as f:
            return await f.read()

    async def delete(self, key: str, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        cache_path = AsyncPath(self.base) / region / key
        if await cache_path.exists():
            await cache_path.unlink()

    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区，为None时清空所有区缓存
        """
        if region:
            # 清理指定缓存区
            cache_path = AsyncPath(self.base) / region
            if await cache_path.exists():
                async for item in cache_path.iterdir():
                    if await item.is_file():
                        await item.unlink()
                    else:
                        await aioshutil.rmtree(item, ignore_errors=True)
        else:
            # 清除所有区域的缓存
            async for item in AsyncPath(self.base).iterdir():
                if await item.is_file():
                    await item.unlink()
                else:
                    await aioshutil.rmtree(item, ignore_errors=True)

    async def items(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        获取指定区域的所有缓存项

        :param region: 缓存的区
        :return: 返回一个字典，包含所有缓存键值对
        """
        cache_path = AsyncPath(self.base) / region
        if not await cache_path.exists():
            yield "", None
            return
        async for item in cache_path.iterdir():
            if await item.is_file():
                async with aiofiles.open(item, 'r') as f:
                    yield item.as_posix(), await f.read()

    async def close(self) -> None:
        """
        关闭 Redis 客户端的连接池
        """
        pass


def FileCache(base: Path = settings.TEMP_PATH, ttl: Optional[int] = None) -> CacheBackend:
    """
    获取文件缓存后端实例（Redis或文件系统），ttl仅在Redis环境中有效
    """
    if settings.CACHE_BACKEND_TYPE == "redis":
        # 如果使用 Redis，则设置缓存的存活时间为配置的天数转换为秒
        return RedisBackend(ttl=ttl or settings.TEMP_FILE_DAYS * 24 * 3600)
    else:
        # 如果使用文件系统，在停止服务时会自动清理过期文件
        return FileBackend(base=base)


def AsyncFileCache(base: Path = settings.TEMP_PATH, ttl: Optional[int] = None) -> AsyncCacheBackend:
    """
    获取文件异步缓存后端实例（Redis或文件系统），ttl仅在Redis环境中有效
    """
    if settings.CACHE_BACKEND_TYPE == "redis":
        # 如果使用 Redis，则设置缓存的存活时间为配置的天数转换为秒
        return AsyncRedisBackend(ttl=ttl or settings.TEMP_FILE_DAYS * 24 * 3600)
    else:
        # 如果使用文件系统，在停止服务时会自动清理过期文件
        return AsyncFileBackend(base=base)


def Cache(cache_type: Literal['ttl', 'lru'] = 'ttl',
          maxsize: Optional[int] = None,
          ttl: Optional[int] = None) -> CacheBackend:
    """
    根据配置获取缓存后端实例（内存或Redis），maxsize仅在未启用Redis时生效

    :param cache_type: 缓存类型，仅使用内存缓存时生效，支持 'ttl'（默认）和 'lru'
    :param maxsize: 缓存的最大条目数，仅使用cachetools时生效
    :param ttl: 缓存的默认存活时间，单位秒
    :return: 返回缓存后端实例
    """
    if settings.CACHE_BACKEND_TYPE == "redis":
        return RedisBackend(ttl=ttl)
    else:
        # 使用内存缓存，maxsize需要有值
        return MemoryBackend(cache_type=cache_type, maxsize=maxsize, ttl=ttl)


def AsyncCache(cache_type: Literal['ttl', 'lru'] = 'ttl',
               maxsize: Optional[int] = None,
               ttl: Optional[int] = None) -> AsyncCacheBackend:
    """
    根据配置获取异步缓存后端实例（内存或Redis），maxsize仅在未启用Redis时生效

    :param cache_type: 缓存类型，仅使用内存缓存时生效，支持 'ttl'（默认）和 'lru'
    :param maxsize: 缓存的最大条目数，仅使用cachetools时生效
    :param ttl: 缓存的默认存活时间，单位秒
    :return: 返回异步缓存后端实例
    """
    if settings.CACHE_BACKEND_TYPE == "redis":
        return AsyncRedisBackend(ttl=ttl)
    else:
        # 使用异步内存缓存，maxsize需要有值
        return AsyncMemoryBackend(cache_type=cache_type, maxsize=maxsize, ttl=ttl)


def cached(region: Optional[str] = None, maxsize: Optional[int] = 1024, ttl: Optional[int] = None,
           skip_none: Optional[bool] = True, skip_empty: Optional[bool] = False):
    """
    自定义缓存装饰器，支持为每个 key 动态传递 maxsize 和 ttl

    :param region: 缓存的区
    :param maxsize: 缓存的最大条目数
    :param ttl: 缓存的存活时间，单位秒，未传入则为永久缓存，单位秒
    :param skip_none: 跳过 None 缓存，默认为 True
    :param skip_empty: 跳过空值缓存（如 None, [], {}, "", set()），默认为 False
    :return: 装饰器函数
    """

    def decorator(func):
        # 检查是否为异步函数
        is_async = inspect.iscoroutinefunction(func)

        # 根据函数类型选择对应的缓存后端，没有ttl时默认是 LRU 缓存，否则是 TTL 缓存
        if is_async:
            # 异步函数使用异步缓存后端
            cache_backend = AsyncCache(cache_type="ttl" if ttl else "lru", maxsize=maxsize, ttl=ttl)
        else:
            # 同步函数使用同步缓存后端
            cache_backend = Cache(cache_type="ttl" if ttl else "lru", maxsize=maxsize, ttl=ttl)

        def should_cache(value: Any) -> bool:
            """
            判断是否应该缓存结果，如果返回值是 None 或空值则不缓存

            :param value: 要判断的缓存值
            :return: 是否缓存结果
            """
            if skip_none and value is None:
                return False
            # if skip_empty and value in [None, [], {}, "", set()]:
            if skip_empty and not value:
                return False
            return True

        def is_valid_cache_value(_cache_key: str, _cached_value: Any, _cache_region: str) -> bool:
            """
            判断指定的值是否为一个有效的缓存值

            :param _cache_key: 缓存的键
            :param _cached_value: 缓存的值
            :param _cache_region: 缓存的区
            :return: 若值是有效的缓存值返回 True，否则返回 False
            """
            # 如果 skip_none 为 False，且 value 为 None，需要判断缓存实际是否存在
            if not skip_none and _cached_value is None:
                if not cache_backend.exists(key=_cache_key, region=_cache_region):
                    return False
            return True

        async def async_is_valid_cache_value(_cache_key: str, _cached_value: Any, _cache_region: str) -> bool:
            """
            判断指定的值是否为一个有效的缓存值（异步版本）

            :param _cache_key: 缓存的键
            :param _cached_value: 缓存的值
            :param _cache_region: 缓存的区
            :return: 若值是有效的缓存值返回 True，否则返回 False
            """
            # 如果 skip_none 为 False，且 value 为 None，需要判断缓存实际是否存在
            if not skip_none and _cached_value is None:
                if not await cache_backend.exists(key=_cache_key, region=_cache_region):
                    return False
            return True

        def __get_cache_key(args, kwargs) -> str:
            """
            根据函数和参数生成缓存键

            :param args: 位置参数
            :param kwargs: 关键字参数
            :return: 缓存键
            """
            signature = inspect.signature(func)
            # 绑定传入的参数并应用默认值
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            # 忽略第一个参数，如果它是实例(self)或类(cls)
            parameters = list(signature.parameters.keys())
            if parameters and parameters[0] in ("self", "cls"):
                bound.arguments.pop(parameters[0], None)
            # 按照函数签名顺序提取参数值列表
            keys = [
                bound.arguments[param] for param in signature.parameters if param in bound.arguments
            ]
            # 使用有序参数生成缓存键
            return f"{func.__name__}_{hashkey(*keys)}"

        # 获取缓存区
        cache_region = region if region is not None else f"{func.__module__}.{func.__name__}"

        # 检查是否为异步函数
        is_async = inspect.iscoroutinefunction(func)

        if is_async:
            # 异步函数的缓存装饰器
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                # 获取缓存键
                cache_key = __get_cache_key(args, kwargs)
                # 尝试获取缓存
                cached_value = await cache_backend.get(cache_key, region=cache_region)
                if should_cache(cached_value) and await async_is_valid_cache_value(cache_key, cached_value,
                                                                                   cache_region):
                    return cached_value
                # 执行异步函数并缓存结果
                result = await func(*args, **kwargs)
                # 判断是否需要缓存
                if not should_cache(result):
                    return result
                # 设置缓存（如果有传入的 maxsize 和 ttl，则覆盖默认值）
                await cache_backend.set(cache_key, result, ttl=ttl, maxsize=maxsize, region=cache_region)
                return result

            async def cache_clear():
                """
                清理缓存区
                """
                await cache_backend.clear(region=cache_region)

            async_wrapper.cache_region = cache_region
            async_wrapper.cache_clear = cache_clear
            return async_wrapper
        else:
            # 同步函数的缓存装饰器
            @wraps(func)
            def wrapper(*args, **kwargs):
                # 获取缓存键
                cache_key = __get_cache_key(args, kwargs)
                # 尝试获取缓存
                cached_value = cache_backend.get(cache_key, region=cache_region)
                if should_cache(cached_value) and is_valid_cache_value(cache_key, cached_value, cache_region):
                    return cached_value
                # 执行函数并缓存结果
                result = func(*args, **kwargs)
                # 判断是否需要缓存
                if not should_cache(result):
                    return result
                # 设置缓存（如果有传入的 maxsize 和 ttl，则覆盖默认值）
                cache_backend.set(cache_key, result, ttl=ttl, maxsize=maxsize, region=cache_region)
                return result

            def cache_clear():
                """
                清理缓存区
                """
                cache_backend.clear(region=cache_region)

            wrapper.cache_region = cache_region
            wrapper.cache_clear = cache_clear
            return wrapper

    return decorator


class CacheProxy:
    """
    缓存代理类，将缓存后端的方法直接代理到实例上
    """

    def __init__(self, cache_backend: CacheBackend, region: str):
        """
        初始化缓存代理

        :param cache_backend: 缓存后端实例
        :param region: 缓存区域
        """
        self._cache_backend = cache_backend
        self._region = region

    def __getitem__(self, key):
        """
        获取缓存项
        """
        value = self._cache_backend.get(key, region=self._region)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        """
        设置缓存项
        """
        kwargs = {'region': self._region}
        self._cache_backend.set(key, value, **kwargs)

    def __delitem__(self, key):
        """
        删除缓存项
        """
        if not self._cache_backend.exists(key, region=self._region):
            raise KeyError(key)
        self._cache_backend.delete(key, region=self._region)

    def __contains__(self, key):
        """
        检查键是否存在
        """
        return self._cache_backend.exists(key, region=self._region)

    def __iter__(self):
        """
        返回缓存的迭代器
        """
        for key, _ in self._cache_backend.items(region=self._region):
            yield key

    def __len__(self):
        """
        返回缓存项的数量
        """
        return sum(1 for _ in self._cache_backend.items(region=self._region))

    def is_redis(self) -> bool:
        """
        检查当前缓存后端是否为 Redis
        """
        return self._cache_backend.is_redis()

    def get(self, key: str, **kwargs) -> Any:
        """
        获取缓存值
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.get(key, **kwargs)

    def set(self, key: str, value: Any, **kwargs) -> None:
        """
        设置缓存值
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.set(key, value, **kwargs)

    def delete(self, key: str, **kwargs) -> None:
        """
        删除缓存值
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.delete(key, **kwargs)

    def exists(self, key: str, **kwargs) -> bool:
        """
        检查缓存键是否存在
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.exists(key, **kwargs)

    def clear(self, **kwargs) -> None:
        """
        清除缓存
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.clear(**kwargs)

    def items(self, **kwargs):
        """
        获取所有缓存项
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.items(**kwargs)

    def keys(self, **kwargs):
        """
        获取所有缓存键
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.keys(**kwargs)

    def values(self, **kwargs):
        """
        获取所有缓存值
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.values(**kwargs)

    def update(self, other: Dict[str, Any], **kwargs) -> None:
        """
        更新缓存
        """
        kwargs.setdefault('region', self._region)
        self._cache_backend.update(other, **kwargs)

    def pop(self, key: str, default: Any = None, **kwargs) -> Any:
        """
        弹出缓存项
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.pop(key, default, **kwargs)

    def popitem(self, **kwargs) -> Tuple[str, Any]:
        """
        弹出最后一个缓存项
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.popitem(**kwargs)

    def setdefault(self, key: str, default: Any = None, **kwargs) -> Any:
        """
        设置默认值
        """
        kwargs.setdefault('region', self._region)
        return self._cache_backend.setdefault(key, default, **kwargs)

    def close(self) -> None:
        """
        关闭缓存连接
        """
        self._cache_backend.close()


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
        初始化 TTL 缓存

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
        初始化 LRU 缓存

        :param maxsize: 缓存的最大条目数
        :param region: 缓存的区，为 None 时使用默认区
        """
        super().__init__(Cache(cache_type='lru', maxsize=maxsize), region)
