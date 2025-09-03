import json
import pickle
from typing import Any, Optional, Generator, Tuple, AsyncGenerator, Union
from urllib.parse import quote

import redis
from redis.asyncio import Redis

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.schemas import ConfigChangeEventData
from app.schemas.types import EventType
from app.utils.singleton import Singleton

# 类型缓存集合，针对非容器简单类型
_complex_serializable_types = set()
_simple_serializable_types = set()

# 默认连接参数
_socket_timeout = 30
_socket_connect_timeout = 5
_health_check_interval = 60


def serialize(value: Any) -> bytes:
    """
    将值序列化为二进制数据，根据序列化方式标识格式
    """

    def _is_container_type(t):
        """
        判断是否为容器类型
        """
        return t in (list, dict, tuple, set)

    vt = type(value)
    # 针对非容器类型使用缓存策略
    if not _is_container_type(vt):
        # 如果已知需要复杂序列化
        if vt in _complex_serializable_types:
            return b"PICKLE" + b"\x00" + pickle.dumps(value)
        # 如果已知可以简单序列化
        if vt in _simple_serializable_types:
            json_data = json.dumps(value).encode("utf-8")
            return b"JSON" + b"\x00" + json_data
        # 对于未知的非容器类型，尝试简单序列化，如抛出异常，再使用复杂序列化
        try:
            json_data = json.dumps(value).encode("utf-8")
            _simple_serializable_types.add(vt)
            return b"JSON" + b"\x00" + json_data
        except TypeError:
            _complex_serializable_types.add(vt)
            return b"PICKLE" + b"\x00" + pickle.dumps(value)
    else:
        # 针对容器类型，每次尝试简单序列化，不使用缓存
        try:
            json_data = json.dumps(value).encode("utf-8")
            return b"JSON" + b"\x00" + json_data
        except TypeError:
            return b"PICKLE" + b"\x00" + pickle.dumps(value)


def deserialize(value: bytes) -> Any:
    """
    将二进制数据反序列化为原始值，根据格式标识区分序列化方式
    """
    format_marker, data = value.split(b"\x00", 1)
    if format_marker == b"JSON":
        return json.loads(data.decode("utf-8"))
    elif format_marker == b"PICKLE":
        return pickle.loads(data)
    else:
        raise ValueError("Unknown serialization format")


class RedisHelper(metaclass=Singleton):
    """
    Redis连接和操作助手类，单例模式
    
    特性：
    - 管理Redis连接池和客户端
    - 提供序列化和反序列化功能
    - 支持内存限制和淘汰策略设置
    - 提供键名生成和区域管理功能
    """

    def __init__(self):
        """
        初始化Redis助手实例
        """
        self.redis_url = settings.CACHE_BACKEND_URL
        self.client = None

    def _connect(self):
        """
        建立Redis连接
        """
        try:
            if self.client is None:
                self.client = redis.Redis.from_url(
                    self.redis_url,
                    decode_responses=False,
                    socket_timeout=_socket_timeout,
                    socket_connect_timeout=_socket_connect_timeout,
                    health_check_interval=_health_check_interval,
                )
                # 测试连接，确保Redis可用
                self.client.ping()
                logger.info(f"Successfully connected to Redis：{self.redis_url}")
                self.set_memory_limit()
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.client = None
            raise RuntimeError("Redis connection failed") from e

    @eventmanager.register(EventType.ConfigChanged)
    def handle_config_changed(self, event: Event):
        """
        处理配置变更事件，更新Redis设置
        :param event: 事件对象
        """
        if not event:
            return
        event_data: ConfigChangeEventData = event.event_data
        if event_data.key not in ['CACHE_BACKEND_TYPE', 'CACHE_BACKEND_URL', 'CACHE_REDIS_MAXMEMORY']:
            return
        logger.info("配置变更，重连Redis...")
        self.close()
        self._connect()

    def set_memory_limit(self, policy: Optional[str] = "allkeys-lru"):
        """
        动态设置Redis最大内存和内存淘汰策略
        
        :param policy: 淘汰策略（如'allkeys-lru'）
        """
        try:
            # 如果有显式值，则直接使用，为0时说明不限制，如果未配置，开启BIG_MEMORY_MODE时为"1024mb"，未开启时为"256mb"
            maxmemory = settings.CACHE_REDIS_MAXMEMORY or ("1024mb" if settings.BIG_MEMORY_MODE else "256mb")
            self.client.config_set("maxmemory", maxmemory)
            self.client.config_set("maxmemory-policy", policy)
            logger.debug(f"Redis maxmemory set to {maxmemory}, policy: {policy}")
        except Exception as e:
            logger.error(f"Failed to set Redis maxmemory or policy: {e}")

    @staticmethod
    def __get_region(region: Optional[str] = None):
        """
        获取缓存的区
        """
        return f"region:{quote(region)}" if region else "region:DEFAULT"

    def __make_redis_key(self, region: str, key: str) -> str:
        """
        获取缓存Key
        """
        # 使用region作为缓存键的一部分
        region = self.__get_region(region)
        return f"{region}:key:{quote(key)}"

    @staticmethod
    def __get_original_key(redis_key: Union[str, bytes]) -> str:
        """
        从Redis键中提取原始key
        """
        try:
            if isinstance(redis_key, bytes):
                redis_key = redis_key.decode("utf-8")
            parts = redis_key.split(":key:")
            return parts[-1]
        except Exception as e:
            logger.warn(f"Failed to parse redis key: {redis_key}, error: {e}")
            return redis_key

    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            region: Optional[str] = "DEFAULT", **kwargs) -> None:
        """
        设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，单位秒
        :param region: 缓存的区
        :param kwargs: 其他参数
        """
        try:
            self._connect()
            redis_key = self.__make_redis_key(region, key)
            # 对值进行序列化
            serialized_value = serialize(value)
            kwargs.pop("maxsize", None)
            self.client.set(redis_key, serialized_value, ex=ttl, **kwargs)
        except Exception as e:
            logger.error(f"Failed to set key: {key} in region: {region}, error: {e}")

    def exists(self, key: str, region: Optional[str] = "DEFAULT") -> bool:
        """
        判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回True，否则返回False
        """
        try:
            self._connect()
            redis_key = self.__make_redis_key(region, key)
            return self.client.exists(redis_key) == 1
        except Exception as e:
            logger.error(f"Failed to exists key: {key} region: {region}, error: {e}")
            return False

    def get(self, key: str, region: Optional[str] = "DEFAULT") -> Optional[Any]:
        """
        获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回None
        """
        try:
            self._connect()
            redis_key = self.__make_redis_key(region, key)
            value = self.client.get(redis_key)
            if value is not None:
                return deserialize(value)
            return None
        except Exception as e:
            logger.error(f"Failed to get key: {key} in region: {region}, error: {e}")
            return None

    def delete(self, key: str, region: Optional[str] = "DEFAULT") -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        try:
            self._connect()
            redis_key = self.__make_redis_key(region, key)
            self.client.delete(redis_key)
        except Exception as e:
            logger.error(f"Failed to delete key: {key} in region: {region}, error: {e}")

    def clear(self, region: Optional[str] = None) -> None:
        """
        清除指定区域的缓存或全部缓存

        :param region: 缓存的区
        """
        try:
            self._connect()
            if region:
                cache_region = self.__get_region(region)
                redis_key = f"{cache_region}:key:*"
                with self.client.pipeline() as pipe:
                    for key in self.client.scan_iter(redis_key):
                        pipe.delete(key)
                    pipe.execute()
                logger.info(f"Cleared Redis cache for region: {region}")
            else:
                self.client.flushdb()
                logger.info("Cleared all Redis cache")
        except Exception as e:
            logger.error(f"Failed to clear cache, region: {region}, error: {e}")

    def items(self, region: Optional[str] = None) -> Generator[Tuple[str, Any], None, None]:
        """
        获取指定区域的所有缓存键值对

        :param region: 缓存的区
        :return: 返回键值对生成器
        """
        try:
            self._connect()
            if region:
                cache_region = self.__get_region(region)
                redis_key = f"{cache_region}:key:*"
                for key in self.client.scan_iter(redis_key):
                    value = self.client.get(key)
                    if value is not None:
                        yield self.__get_original_key(key), deserialize(value)
            else:
                for key in self.client.scan_iter("*"):
                    value = self.client.get(key)
                    if value is not None:
                        yield self.__get_original_key(key), deserialize(value)
        except Exception as e:
            logger.error(f"Failed to get items from Redis, region: {region}, error: {e}")

    def test(self) -> bool:
        """
        测试Redis连接性
        """
        try:
            self._connect()
            return True
        except Exception as e:
            logger.error(f"Redis connection test failed: {e}")
            return False

    def close(self) -> None:
        """
        关闭Redis客户端的连接池
        """
        if self.client:
            self.client.close()
            self.client = None
            logger.debug("Redis connection closed")


class AsyncRedisHelper(metaclass=Singleton):
    """
    异步Redis连接和操作助手类，单例模式
    
    特性：
    - 管理异步Redis连接池和客户端
    - 提供序列化和反序列化功能
    - 支持内存限制和淘汰策略设置
    - 提供键名生成和区域管理功能
    - 所有操作都是异步的
    """

    def __init__(self):
        """
        初始化异步Redis助手实例
        """
        self.redis_url = settings.CACHE_BACKEND_URL
        self.client: Optional[Redis] = None

    async def _connect(self):
        """
        建立异步Redis连接
        """
        try:
            if self.client is None:
                self.client = Redis.from_url(
                    self.redis_url,
                    decode_responses=False,
                    socket_timeout=_socket_timeout,
                    socket_connect_timeout=_socket_connect_timeout,
                    health_check_interval=_health_check_interval,
                )
                # 测试连接，确保Redis可用
                await self.client.ping()
                logger.info(f"Successfully connected to Redis (async)：{self.redis_url}")
                await self.set_memory_limit()
        except Exception as e:
            logger.error(f"Failed to connect to Redis (async): {e}")
            self.client = None
            raise RuntimeError("Redis async connection failed") from e

    @eventmanager.register(EventType.ConfigChanged)
    async def handle_config_changed(self, event: Event):
        """
        处理配置变更事件，更新Redis设置
        :param event: 事件对象
        """
        if not event:
            return
        event_data: ConfigChangeEventData = event.event_data
        if event_data.key not in ['CACHE_BACKEND_TYPE', 'CACHE_BACKEND_URL', 'CACHE_REDIS_MAXMEMORY']:
            return
        logger.info("配置变更，重连Redis (async)...")
        await self.close()
        await self._connect()

    async def set_memory_limit(self, policy: Optional[str] = "allkeys-lru"):
        """
        动态设置Redis最大内存和内存淘汰策略
        
        :param policy: 淘汰策略（如'allkeys-lru'）
        """
        try:
            # 如果有显式值，则直接使用，为0时说明不限制，如果未配置，开启BIG_MEMORY_MODE时为"1024mb"，未开启时为"256mb"
            maxmemory = settings.CACHE_REDIS_MAXMEMORY or ("1024mb" if settings.BIG_MEMORY_MODE else "256mb")
            await self.client.config_set("maxmemory", maxmemory)
            await self.client.config_set("maxmemory-policy", policy)
            logger.debug(f"Redis maxmemory set to {maxmemory}, policy: {policy} (async)")
        except Exception as e:
            logger.error(f"Failed to set Redis maxmemory or policy (async): {e}")

    @staticmethod
    def __get_region(region: Optional[str] = "DEFAULT"):
        """
        获取缓存的区
        """
        return f"region:{region}" if region else "region:default"

    def __make_redis_key(self, region: str, key: str) -> str:
        """
        获取缓存Key
        """
        # 使用region作为缓存键的一部分
        region = self.__get_region(region)
        return f"{region}:key:{quote(key)}"

    @staticmethod
    def __get_original_key(redis_key: Union[str, bytes]) -> str:
        """
        从Redis键中提取原始key
        """
        try:
            if isinstance(redis_key, bytes):
                redis_key = redis_key.decode("utf-8")
            parts = redis_key.split(":key:")
            return parts[-1]
        except Exception as e:
            logger.warn(f"Failed to parse redis key: {redis_key}, error: {e}")
            return redis_key

    async def set(self, key: str, value: Any, ttl: Optional[int] = None,
                  region: Optional[str] = "DEFAULT", **kwargs) -> None:
        """
        异步设置缓存

        :param key: 缓存的键
        :param value: 缓存的值
        :param ttl: 缓存的存活时间，单位秒
        :param region: 缓存的区
        :param kwargs: 其他参数
        """
        try:
            await self._connect()
            redis_key = self.__make_redis_key(region, key)
            # 对值进行序列化
            serialized_value = serialize(value)
            kwargs.pop("maxsize", None)
            await self.client.set(redis_key, serialized_value, ex=ttl, **kwargs)
        except Exception as e:
            logger.error(f"Failed to set key (async): {key} in region: {region}, error: {e}")

    async def exists(self, key: str, region: Optional[str] = "DEFAULT") -> bool:
        """
        异步判断缓存键是否存在

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 存在返回True，否则返回False
        """
        try:
            await self._connect()
            redis_key = self.__make_redis_key(region, key)
            result = await self.client.exists(redis_key)
            return result == 1
        except Exception as e:
            logger.error(f"Failed to exists key (async): {key} region: {region}, error: {e}")
            return False

    async def get(self, key: str, region: Optional[str] = "DEFAULT") -> Optional[Any]:
        """
        异步获取缓存的值

        :param key: 缓存的键
        :param region: 缓存的区
        :return: 返回缓存的值，如果缓存不存在返回None
        """
        try:
            await self._connect()
            redis_key = self.__make_redis_key(region, key)
            value = await self.client.get(redis_key)
            if value is not None:
                return deserialize(value)
            return None
        except Exception as e:
            logger.error(f"Failed to get key (async): {key} in region: {region}, error: {e}")
            return None

    async def delete(self, key: str, region: Optional[str] = "DEFAULT") -> None:
        """
        异步删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        try:
            await self._connect()
            redis_key = self.__make_redis_key(region, key)
            await self.client.delete(redis_key)
        except Exception as e:
            logger.error(f"Failed to delete key (async): {key} in region: {region}, error: {e}")

    async def clear(self, region: Optional[str] = None) -> None:
        """
        异步清除指定区域的缓存或全部缓存

        :param region: 缓存的区
        """
        try:
            await self._connect()
            if region:
                cache_region = self.__get_region(region)
                redis_key = f"{cache_region}:key:*"
                async with self.client.pipeline() as pipe:
                    async for key in self.client.scan_iter(redis_key):
                        await pipe.delete(key)
                    await pipe.execute()
                logger.info(f"Cleared Redis cache for region (async): {region}")
            else:
                await self.client.flushdb()
                logger.info("Cleared all Redis cache (async)")
        except Exception as e:
            logger.error(f"Failed to clear cache (async), region: {region}, error: {e}")

    async def items(self, region: Optional[str] = None) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        获取指定区域的所有缓存键值对

        :param region: 缓存的区
        :return: 返回键值对生成器
        """
        try:
            await self._connect()
            if region:
                cache_region = self.__get_region(region)
                redis_key = f"{cache_region}:key:*"
                async for key in self.client.scan_iter(redis_key):
                    value = await self.client.get(key)
                    if value is not None:
                        yield self.__get_original_key(key), deserialize(value)
            else:
                async for key in self.client.scan_iter("*"):
                    value = await self.client.get(key)
                    if value is not None:
                        yield self.__get_original_key(key), deserialize(value)
        except Exception as e:
            logger.error(f"Failed to get items from Redis, region: {region}, error: {e}")

    async def test(self) -> bool:
        """
        异步测试Redis连接性
        """
        try:
            await self._connect()
            return True
        except Exception as e:
            logger.error(f"Redis async connection test failed: {e}")
            return False

    async def close(self) -> None:
        """
        关闭异步Redis客户端的连接池
        """
        if self.client:
            await self.client.close()
            self.client = None
            logger.debug("Redis async connection closed")
