import inspect
import shutil
import tempfile
import threading
from abc import ABC, abstractmethod
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional, Generator, AsyncGenerator, Tuple

import aiofiles
import aioshutil
from anyio import Path as AsyncPath
from cachetools import TTLCache as MemoryTTLCache
from cachetools.keys import hashkey

from app.core.config import settings
from app.helper.redis import RedisHelper, AsyncRedisHelper
from app.log import logger

# 默认缓存区
DEFAULT_CACHE_REGION = "DEFAULT"

lock = threading.Lock()


class CacheBackend(ABC):
    """
    缓存后端基类，定义通用的缓存接口
    """

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

    @abstractmethod
    def close(self) -> None:
        """
        关闭缓存连接
        """
        pass

    @staticmethod
    def get_region(region: Optional[str] = DEFAULT_CACHE_REGION):
        """
        获取缓存的区
        """
        return f"region:{region}" if region else "region:default"

    @staticmethod
    def get_cache_key(func, args, kwargs):
        """
        获取缓存的键，通过哈希函数对函数的参数进行处理
        :param func: 被装饰的函数
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

    @staticmethod
    def is_redis() -> bool:
        return settings.CACHE_BACKEND_TYPE == "redis"


class AsyncCacheBackend(ABC):
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

    @abstractmethod
    async def close(self) -> None:
        """
        关闭缓存连接
        """
        pass

    @staticmethod
    def get_region(region: Optional[str] = DEFAULT_CACHE_REGION):
        """
        获取缓存的区
        """
        return f"region:{region}" if region else "region:default"

    @staticmethod
    def get_cache_key(func, args, kwargs):
        """
        获取缓存的键，通过哈希函数对函数的参数进行处理
        :param func: 被装饰的函数
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

    @staticmethod
    def is_redis() -> bool:
        return settings.CACHE_BACKEND_TYPE == "redis"


class MemoryBackend(CacheBackend):
    """
    基于 `cachetools.TTLCache` 实现的缓存后端
    """

    def __init__(self, maxsize: Optional[int] = None, ttl: Optional[int] = None):
        """
        初始化缓存实例

        :param maxsize: 缓存的最大条目数
        :param ttl: 默认缓存存活时间，单位秒
        """
        self.maxsize = maxsize or 1024  # 未设置时默认最大条目数为 1024
        self.ttl = ttl
        # 存储各个 region 的缓存实例，region -> TTLCache
        self._region_caches: Dict[str, MemoryTTLCache] = {}

    def __get_region_cache(self, region: str) -> Optional[MemoryTTLCache]:
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
        # 如果该 key 尚未有缓存实例，则创建一个新的 TTLCache 实例
        region_cache = self._region_caches.setdefault(region, MemoryTTLCache(maxsize=maxsize, ttl=ttl))
        # 设置缓存值
        with lock:
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
                logger.info(f"Cleared cache for region: {region}")
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
        for item in region_cache.items():
            yield item

    def close(self) -> None:
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
                    yield item.name, f.read()

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
                    yield item.name, await f.read()

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


def Cache(maxsize: Optional[int] = None, ttl: Optional[int] = None) -> CacheBackend:
    """
    根据配置获取缓存后端实例（内存或Redis），maxsize仅在未启用Redis时生效

    :param maxsize: 缓存的最大条目数，仅使用cachetools时生效
    :param ttl: 缓存的默认存活时间，单位秒
    :return: 返回缓存后端实例
    """
    if settings.CACHE_BACKEND_TYPE == "redis":
        return RedisBackend(ttl=ttl)
    else:
        # 使用内存缓存，maxsize需要有值
        return MemoryBackend(maxsize=maxsize, ttl=ttl)


class TTLCache:
    """
    TTL缓存类，根据配置自动选择使用Redis或cachetools，maxsize仅在未启用Redis时生效

    特性：
    - 提供与cachetools.TTLCache相同的接口
    - 根据配置自动选择缓存后端
    - 支持Redis和cachetools的切换
    """

    def __init__(self, region: Optional[str] = DEFAULT_CACHE_REGION,
                 maxsize: int = None, ttl: int = None):
        """
        初始化TTL缓存

        :param region: 缓存的区，默认为 DEFAULT_CACHE_REGION
        :param maxsize: 缓存的最大条目数
        :param ttl: 缓存的存活时间，单位秒
        """
        self.region = region
        self.maxsize = maxsize
        self.ttl = ttl
        self._backend = Cache(maxsize=maxsize, ttl=ttl)

    def __getitem__(self, key: str):
        """
        获取缓存项
        """
        try:
            value = self._backend.get(key, region=self.region)
            if value is not None:
                return value
        except Exception as e:
            logger.warning(f"缓存获取失败: {e}")

        raise KeyError(key)

    def __setitem__(self, key: str, value: Any):
        """
        设置缓存项
        """
        try:
            self._backend.set(key, value, ttl=self.ttl, region=self.region)
        except Exception as e:
            logger.warning(f"缓存设置失败: {e}")

    def __delitem__(self, key: str):
        """
        删除缓存项
        """
        try:
            self._backend.delete(key, region=self.region)
        except Exception as e:
            logger.warning(f"缓存删除失败: {e}")

    def __contains__(self, key: str):
        """
        检查键是否存在
        """
        try:
            return self._backend.exists(key, region=self.region)
        except Exception as e:
            logger.warning(f"缓存检查失败: {e}")
            return False

    def __iter__(self):
        """
        返回缓存的迭代器
        """
        for key, _ in self._backend.items(region=self.region):
            yield key

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """
        设置缓存项，支持自定义 TTL
        """
        try:
            ttl = ttl or self.ttl
            self._backend.set(key, value, ttl=ttl, region=self.region)
        except Exception as e:
            logger.warning(f"缓存设置失败: {e}")

    def get(self, key: str, default: Any = None):
        """
        获取缓存项，如果不存在返回默认值
        """
        try:
            value = self._backend.get(key, region=self.region)
            if value is not None:
                return value
        except Exception as e:
            logger.warning(f"缓存获取失败: {e}")

        return default

    def delete(self, key: str):
        """
        删除缓存项
        """
        try:
            self._backend.delete(key, region=self.region)
        except Exception as e:
            logger.warning(f"缓存删除失败: {e}")

    def items(self):
        """
        获取缓存的所有键值对
        """
        try:
            return self._backend.items(region=self.region)
        except Exception as e:
            logger.warning(f"缓存获取失败: {e}")
            return []

    def clear(self):
        """
        清空缓存
        """
        try:
            self._backend.clear(region=self.region)
        except Exception as e:
            logger.warning(f"缓存清空失败: {e}")

    def is_redis(self) -> bool:
        """
        判断当前缓存后端是否为 Redis
        """
        return self._backend.is_redis()

    def close(self):
        """
        关闭缓存连接
        """
        try:
            self._backend.close()
        except Exception as e:
            logger.warning(f"缓存关闭失败: {e}")


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
    # 缓存后端实例
    cache_backend = Cache(maxsize=maxsize, ttl=ttl)

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

    def is_valid_cache_value(cache_key: str, cached_value: Any, cache_region: str) -> bool:
        """
        判断指定的值是否为一个有效的缓存值

        :param cache_key: 缓存的键
        :param cached_value: 缓存的值
        :param cache_region: 缓存的区
        :return: 若值是有效的缓存值返回 True，否则返回 False
        """
        # 如果 skip_none 为 False，且 value 为 None，需要判断缓存实际是否存在
        if not skip_none and cached_value is None:
            if not cache_backend.exists(key=cache_key, region=cache_region):
                return False
        return True

    def decorator(func):

        # 获取缓存区
        cache_region = region if region is not None else f"{func.__module__}.{func.__name__}"

        # 检查是否为异步函数
        is_async = inspect.iscoroutinefunction(func)

        if is_async:
            # 异步函数的缓存装饰器
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                # 获取缓存键
                cache_key = cache_backend.get_cache_key(func, args, kwargs)
                # 尝试获取缓存
                cached_value = cache_backend.get(cache_key, region=cache_region)
                if should_cache(cached_value) and is_valid_cache_value(cache_key, cached_value, cache_region):
                    return cached_value
                # 执行异步函数并缓存结果
                result = await func(*args, **kwargs)
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

            async_wrapper.cache_region = cache_region
            async_wrapper.cache_clear = cache_clear
            return async_wrapper
        else:
            # 同步函数的缓存装饰器
            @wraps(func)
            def wrapper(*args, **kwargs):
                # 获取缓存键
                cache_key = cache_backend.get_cache_key(func, args, kwargs)
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
