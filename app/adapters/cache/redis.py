"""
Redis 连接池、带格式标记的值序列化，以及同步和异步缓存后端。
"""

import asyncio
import json
import pickle
import threading
import weakref
from typing import Any, AsyncGenerator, Generator, Optional, Tuple, Union
from urllib.parse import quote, unquote

import redis
from redis.asyncio import BlockingConnectionPool as AsyncBlockingConnectionPool
from redis.asyncio import Redis

from app.foundation.singleton import Singleton
from app.runtime.cache import DEFAULT_CACHE_REGION, AsyncCacheBackend, AtomicCacheBackend
from app.runtime.log import logger
from app.runtime.reload import ConfigReloadMixin
from app.runtime.settings import get_runtime_setting

# 类型缓存集合，针对非容器简单类型
_complex_serializable_types = set()
_simple_serializable_types = set()

# Redis 命令响应超时，单位为秒
_socket_timeout = 30
# Redis 建立连接的超时，单位为秒
_socket_connect_timeout = 5
# 空闲连接重新使用前的健康检查间隔，单位为秒
_health_check_interval = 60


def serialize(value: Any) -> bytes:
    """
    为缓存值添加格式标记，并序列化为字节。

    先尝试 JSON，不能编码时回退到 Pickle；非容器类型会缓存可用格式，容器每次重新尝试。
    JSON 支持的值沿用 JSON 的类型转换规则，不保证还原全部 Python 原始类型。

    :param value: 要保存的 Python 对象
    :return: 格式名、零字节分隔符和编码内容组成的缓存数据
    """

    def _is_container_type(t: type[object]) -> bool:
        """
        判断是否为容器类型
        """
        return t in (list, dict, tuple, set)

    vt = type(value)
    # 针对非容器类型使用缓存策略
    if not _is_container_type(vt):
        if vt in _complex_serializable_types:
            return b"PICKLE" + b"\x00" + pickle.dumps(value)
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
    按格式标记解码缓存值，仅供本系统写入的可信缓存数据使用。

    :param value: serialize 生成的带格式标记字节；Pickle 内容不应来自不可信来源
    :return: JSON 或 Pickle 解码后的对象
    :raises ValueError: 缺少分隔符或格式标记未知；无效编码也会传播对应解码异常
    """
    format_marker, data = value.split(b"\x00", 1)
    if format_marker == b"JSON":
        return json.loads(data.decode("utf-8"))
    elif format_marker == b"PICKLE":
        return pickle.loads(data)
    else:
        raise ValueError("Unknown serialization format")


class RedisHelper(ConfigReloadMixin, metaclass=Singleton):
    """
    管理进程共享的同步 Redis 连接池，提供容错读写与严格的一次性值领取接口。
    """
    CONFIG_WATCH = {
        "CACHE_BACKEND_TYPE",
        "CACHE_BACKEND_URL",
        "CACHE_REDIS_MAXMEMORY",
        "CACHE_REDIS_MAX_CONNECTIONS",
        "CACHE_REDIS_POOL_TIMEOUT",
        "BIG_MEMORY_MODE",
    }

    def __init__(self) -> None:
        """
        初始化Redis助手实例
        """
        self.redis_url = get_runtime_setting('CACHE_BACKEND_URL')
        self.client: Optional[redis.Redis] = None
        self._connect_lock = threading.RLock()

    def _connect(self) -> redis.Redis:
        """
        建立Redis连接
        """
        if self.client is not None:
            return self.client
        client = None
        try:
            with self._connect_lock:
                if self.client is not None:
                    return self.client
                self.redis_url = get_runtime_setting('CACHE_BACKEND_URL')
                connection_pool = redis.BlockingConnectionPool.from_url(
                    self.redis_url,
                    decode_responses=False,
                    socket_timeout=_socket_timeout,
                    socket_connect_timeout=_socket_connect_timeout,
                    health_check_interval=_health_check_interval,
                    max_connections=get_runtime_setting('CACHE_REDIS_MAX_CONNECTIONS'),
                    timeout=get_runtime_setting('CACHE_REDIS_POOL_TIMEOUT'),
                )
                client = redis.Redis(connection_pool=connection_pool)
                # 测试连接，确保Redis可用
                client.ping()
                self.client = client
                logger.info(f"Successfully connected to Redis：{self.redis_url}")
                self.set_memory_limit()
                return client
        except Exception as e:
            if client:
                client.close()
            logger.error(f"Failed to connect to Redis: {e}")
            self.client = None
            raise RuntimeError("Redis connection failed") from e

    def on_config_changed(self) -> None:
        """
        缓存配置变化后重建同步 Redis 连接。
        """
        with self._connect_lock:
            self.redis_url = get_runtime_setting('CACHE_BACKEND_URL')
            self.close()
        self._connect()

    def get_reload_name(self) -> str:
        """
        返回同步 Redis 配置重载名称。
        """
        return "Redis"

    def set_memory_limit(self, policy: Optional[str] = "allkeys-lru") -> None:
        """
        动态设置Redis最大内存和内存淘汰策略

        :param policy: 淘汰策略（如'allkeys-lru'）
        """
        if self.client is None:
            return
        try:
            # 非空配置直接传给 Redis；未配置时按大内存模式选择 1024 MB 或 256 MB。
            maxmemory = get_runtime_setting('CACHE_REDIS_MAXMEMORY') or (
                "1024mb" if get_runtime_setting('BIG_MEMORY_MODE') else "256mb"
            )
            self.client.config_set("maxmemory", maxmemory)
            self.client.config_set("maxmemory-policy", policy)
            logger.debug(f"Redis maxmemory set to {maxmemory}, policy: {policy}")
        except Exception as e:
            logger.error(f"Failed to set Redis maxmemory or policy: {e}")

    @staticmethod
    def __get_region(region: Optional[str] = None) -> str:
        """
        获取缓存的区
        """
        return f"region:{region}" if region else "region:DEFAULT"

    def __make_redis_key(self, region: Optional[str], key: str) -> str:
        """
        获取缓存Key
        """
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
            return unquote(parts[-1])
        except Exception as e:
            logger.warn("解析 Redis 缓存键失败：%r，错误：%s", redis_key, e)
            return redis_key.decode("utf-8", errors="replace") if isinstance(redis_key, bytes) else redis_key

    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            region: Optional[str] = "DEFAULT", **kwargs: Any) -> None:
        """
        序列化并写入缓存，连接或写入异常仅记录日志。

        :param key: 区域内的原始缓存键
        :param value: 交给 serialize 编码的对象
        :param ttl: Redis 有效期，单位为秒；None 不设置过期时间
        :param region: 缓存区域；为空时使用 DEFAULT
        :param kwargs: Redis set 的额外选项；兼容参数 maxsize 会被丢弃
        """
        try:
            self.store(key=key, value=value, ttl=ttl, region=region, **kwargs)
        except Exception as e:
            logger.error(f"Failed to set key: {key} in region: {region}, error: {e}")

    def store(self, key: str, value: Any, ttl: Optional[int] = None,
              region: Optional[str] = "DEFAULT", **kwargs: Any) -> None:
        """
        写入缓存并要求 Redis 确认，连接或序列化故障向调用方传播。

        :param key: 区域内的原始缓存键，编码后用于 Redis 键名
        :param value: 交给 serialize 编码的对象
        :param ttl: 有效期，单位为秒；None 不设置过期时间
        :param region: 缓存区域；为空时使用 DEFAULT
        :param kwargs: Redis set 的额外选项；兼容参数 maxsize 会被丢弃
        :raises RuntimeError: 连接建立失败，或写入未得到 True 确认
        """
        client = self._connect()
        redis_key = self.__make_redis_key(region, key)
        serialized_value = serialize(value)
        kwargs.pop("maxsize", None)
        stored = client.set(redis_key, serialized_value, ex=ttl, **kwargs)
        if stored is not True:
            raise RuntimeError("Redis cache write was not acknowledged")

    def exists(self, key: str, region: Optional[str] = "DEFAULT") -> bool:
        """
        判断区域内是否存在该键，查询故障时记录日志并返回 False。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 键存在时返回 True；不存在或发生故障时返回 False
        """
        try:
            client = self._connect()
            redis_key = self.__make_redis_key(region, key)
            return bool(client.exists(redis_key) == 1)
        except Exception as e:
            logger.error(f"Failed to exists key: {key} region: {region}, error: {e}")
            return False

    def get(self, key: str, region: Optional[str] = "DEFAULT") -> Optional[Any]:
        """
        读取并解码缓存值，查询或解码故障时记录日志并返回 None。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 解码后的值；键不存在或发生故障时返回 None
        """
        try:
            client = self._connect()
            redis_key = self.__make_redis_key(region, key)
            value = client.get(redis_key)
            if value is not None:
                return deserialize(value)
            return None
        except Exception as e:
            logger.error(f"Failed to get key: {key} in region: {region}, error: {e}")
            return None

    def pop(self, key: str, region: Optional[str] = "DEFAULT") -> Optional[Any]:
        """
        兼容旧调用的原子领取入口，故障记录日志后返回 None。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 读取后已删除的值；不存在或发生故障时返回 None
        """
        try:
            return self.consume(key=key, region=region)
        except Exception as e:
            logger.error(
                f"Failed to pop key: {key} in region: {region}, error: {e}"
            )
            return None

    def consume(self, key: str, region: Optional[str] = "DEFAULT") -> Optional[Any]:
        """
        通过单条 GETDEL 命令读取并删除缓存，避免同一值被重复领取。

        删除先于反序列化，解码失败时也不会恢复原值。连接、命令及解码故障向调用方传播。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 领取并解码的值；键不存在时返回 None
        """
        client = self._connect()
        redis_key = self.__make_redis_key(region, key)
        value = client.getdel(redis_key)
        return deserialize(value) if value is not None else None

    def delete(self, key: str, region: Optional[str] = "DEFAULT") -> None:
        """
        删除缓存

        :param key: 缓存的键
        :param region: 缓存的区
        """
        try:
            client = self._connect()
            redis_key = self.__make_redis_key(region, key)
            client.delete(redis_key)
        except Exception as e:
            logger.error(f"Failed to delete key: {key} in region: {region}, error: {e}")

    def clear(self, region: Optional[str] = None) -> None:
        """
        清空选定区域；未指定区域时清空当前 Redis 数据库。

        清理故障仅记录日志，调用结束不保证全部键均已删除。

        :param region: 要清空的区域；None 或空字符串会执行 flushdb，影响当前数据库的全部键
        """
        try:
            client = self._connect()
            if region:
                cache_region = self.__get_region(region)
                redis_key = f"{cache_region}:key:*"
                with client.pipeline() as pipe:
                    for key in client.scan_iter(redis_key):
                        pipe.delete(key)
                    pipe.execute()
                logger.debug(f"Cleared Redis cache for region: {region}")
            else:
                client.flushdb()
                logger.info("All Redis cache Cleared！")
        except Exception as e:
            logger.error(f"Failed to clear cache, region: {region}, error: {e}")

    def items(self, region: Optional[str] = None) -> Generator[Tuple[str, Any], None, None]:
        """
        扫描并逐项读取缓存，遇到故障时记录日志并结束遍历。

        扫描期间可能发生并发修改，不提供一致性快照；整个数据库遍历可能遇到非本系统缓存。

        :param region: 限定区域；None 或空字符串会扫描当前数据库的全部键
        :yields: 原始缓存键与解码后的值组成的二元组
        """
        try:
            client = self._connect()
            if region:
                cache_region = self.__get_region(region)
                redis_key = f"{cache_region}:key:*"
                for key in client.scan_iter(redis_key):
                    value = client.get(key)
                    if value is not None:
                        yield self.__get_original_key(key), deserialize(value)
            else:
                for key in client.scan_iter("*"):
                    value = client.get(key)
                    if value is not None:
                        yield self.__get_original_key(key), deserialize(value)
        except Exception as e:
            logger.error(f"Failed to get items from Redis, region: {region}, error: {e}")

    def test(self) -> bool:
        """
        尝试取得可用的 Redis 客户端，连接失败时记录日志。

        :return: 客户端就绪时返回 True，否则返回 False；已缓存的客户端不会再次发送探测请求
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
        with self._connect_lock:
            if self.client:
                self.client.close()
                self.client = None
                logger.debug("Redis connection closed")


class AsyncRedisHelper(ConfigReloadMixin, metaclass=Singleton):
    """
    按事件循环管理共享 Redis 连接池，避免不同事件循环复用异步客户端。
    """
    CONFIG_WATCH = {
        "CACHE_BACKEND_TYPE",
        "CACHE_BACKEND_URL",
        "CACHE_REDIS_MAXMEMORY",
        "CACHE_REDIS_MAX_CONNECTIONS",
        "CACHE_REDIS_POOL_TIMEOUT",
        "BIG_MEMORY_MODE",
    }

    def __init__(self) -> None:
        """
        初始化异步Redis助手实例
        """
        self.redis_url = get_runtime_setting('CACHE_BACKEND_URL')
        self.client: Optional[Redis] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, Redis
        ] = weakref.WeakKeyDictionary()
        self._connect_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Lock
        ] = weakref.WeakKeyDictionary()
        self._state_lock = threading.RLock()

    def _get_connect_lock(self, current_loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        """
        获取当前事件循环对应的异步连接锁
        """
        with self._state_lock:
            connect_lock = self._connect_locks.get(current_loop)
            if connect_lock is None:
                connect_lock = asyncio.Lock()
                self._connect_locks[current_loop] = connect_lock
            return connect_lock

    async def _close_client_instance(self, client: Redis) -> None:
        """
        在客户端所属事件循环中关闭连接池。
        """
        try:
            close = getattr(client, "aclose", None) or getattr(client, "close")
            await close()
        except Exception:
            pass
        await self._close_client_pool(client)

    async def _close_client_pool(self, client: Redis) -> None:
        """
        关闭异步 Redis 客户端显式持有的连接池。
        """
        pool = getattr(client, "connection_pool", None)
        close_pool = getattr(pool, "aclose", None)
        if close_pool is not None:
            try:
                await close_pool()
            except Exception:
                pass

    async def _connect(self) -> Redis:
        """
        建立异步Redis连接
        """
        current_loop = asyncio.get_running_loop()
        connect_lock = self._get_connect_lock(current_loop)
        client: Optional[Redis] = None
        try:
            async with connect_lock:
                with self._state_lock:
                    client = self._clients.get(current_loop)
                if client is not None:
                    self.client = client
                    self._loop = current_loop
                    return client
                self.redis_url = get_runtime_setting('CACHE_BACKEND_URL')
                connection_pool = AsyncBlockingConnectionPool.from_url(
                    self.redis_url,
                    decode_responses=False,
                    socket_timeout=_socket_timeout,
                    socket_connect_timeout=_socket_connect_timeout,
                    health_check_interval=_health_check_interval,
                    max_connections=get_runtime_setting('CACHE_REDIS_MAX_CONNECTIONS'),
                    timeout=get_runtime_setting('CACHE_REDIS_POOL_TIMEOUT'),
                )
                client = Redis(connection_pool=connection_pool)
                # 测试连接，确保Redis可用
                await client.ping()
                with self._state_lock:
                    self._clients[current_loop] = client
                self.client = client
                self._loop = current_loop
                logger.info(f"Successfully connected to Redis (async)：{self.redis_url}")
                await self.set_memory_limit(client=client)
                return client
        except Exception as e:
            if client:
                await self._close_client_instance(client)
                with self._state_lock:
                    if self._clients.get(current_loop) is client:
                        self._clients.pop(current_loop, None)
                if self.client is client:
                    self.client = None
                    self._loop = None
            logger.error(f"Failed to connect to Redis (async): {e}")
            raise RuntimeError("Redis async connection failed") from e

    async def _close_client(self) -> None:
        """
        关闭当前事件循环拥有的Redis客户端连接
        """
        current_loop = asyncio.get_running_loop()
        connect_lock = self._get_connect_lock(current_loop)
        async with connect_lock:
            with self._state_lock:
                client = self._clients.pop(current_loop, None)
                client_is_current = self.client is client
            if client is not None:
                try:
                    if client_is_current and self.client is client:
                        await self.client.close()
                    else:
                        close = getattr(client, "aclose", None) or getattr(client, "close")
                        await close()
                except Exception:
                    pass
                await self._close_client_pool(client)
                with self._state_lock:
                    if self.client is client:
                        self.client = None
                        self._loop = None

    async def on_config_changed(self) -> None:
        """
        缓存配置变化后异步重建 Redis 连接。
        """
        self.redis_url = get_runtime_setting('CACHE_BACKEND_URL')
        await self._close_client()
        await self._connect()

    def get_reload_name(self) -> str:
        """
        返回异步 Redis 配置重载名称。
        """
        return "Redis (async)"

    async def set_memory_limit(
        self,
        policy: Optional[str] = "allkeys-lru",
        client: Optional[Redis] = None,
    ) -> None:
        """
        动态设置Redis最大内存和内存淘汰策略

        :param policy: 淘汰策略（如'allkeys-lru'）
        :param client: 已建立的异步 Redis 客户端
        """
        if client is None:
            client = await self._connect()
        try:
            # 非空配置直接传给 Redis；未配置时按大内存模式选择 1024 MB 或 256 MB。
            maxmemory = get_runtime_setting('CACHE_REDIS_MAXMEMORY') or (
                "1024mb" if get_runtime_setting('BIG_MEMORY_MODE') else "256mb"
            )
            target_client = self.client if self.client is client else client
            await target_client.config_set("maxmemory", maxmemory)
            await target_client.config_set("maxmemory-policy", policy)
            logger.debug(f"Redis maxmemory set to {maxmemory}, policy: {policy} (async)")
        except Exception as e:
            logger.error(f"Failed to set Redis maxmemory or policy (async): {e}")

    @staticmethod
    def __get_region(region: Optional[str] = "DEFAULT") -> str:
        """
        获取缓存的区
        """
        return f"region:{region}" if region else "region:DEFAULT"

    def __make_redis_key(self, region: Optional[str], key: str) -> str:
        """
        获取缓存Key
        """
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
            return unquote(parts[-1])
        except Exception as e:
            logger.warn("解析 Redis 缓存键失败：%r，错误：%s", redis_key, e)
            return redis_key.decode("utf-8", errors="replace") if isinstance(redis_key, bytes) else redis_key

    async def set(self, key: str, value: Any, ttl: Optional[int] = None,
                  region: Optional[str] = "DEFAULT", **kwargs: Any) -> None:
        """
        序列化并写入缓存，连接或写入异常仅记录日志。

        :param key: 区域内的原始缓存键
        :param value: 交给 serialize 编码的对象
        :param ttl: Redis 有效期，单位为秒；None 不设置过期时间
        :param region: 缓存区域；为空时使用 DEFAULT
        :param kwargs: Redis set 的额外选项；兼容参数 maxsize 会被丢弃
        """
        try:
            current_client = await self._connect()
            client = self.client if self.client is current_client else current_client
            redis_key = self.__make_redis_key(region, key)
            serialized_value = serialize(value)
            kwargs.pop("maxsize", None)
            await client.set(redis_key, serialized_value, ex=ttl, **kwargs)
        except Exception as e:
            logger.error(f"Failed to set key (async): {key} in region: {region}, error: {e}")

    async def exists(self, key: str, region: Optional[str] = "DEFAULT") -> bool:
        """
        判断区域内是否存在该键，查询故障时记录日志并返回 False。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 键存在时返回 True；不存在或发生故障时返回 False
        """
        try:
            current_client = await self._connect()
            client = self.client if self.client is current_client else current_client
            redis_key = self.__make_redis_key(region, key)
            result = await client.exists(redis_key)
            return bool(result == 1)
        except Exception as e:
            logger.error(f"Failed to exists key (async): {key} region: {region}, error: {e}")
            return False

    async def get(self, key: str, region: Optional[str] = "DEFAULT") -> Optional[Any]:
        """
        读取并解码缓存值，查询或解码故障时记录日志并返回 None。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 解码后的值；键不存在或发生故障时返回 None
        """
        try:
            current_client = await self._connect()
            client = self.client if self.client is current_client else current_client
            redis_key = self.__make_redis_key(region, key)
            value = await client.get(redis_key)
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
            current_client = await self._connect()
            client = self.client if self.client is current_client else current_client
            redis_key = self.__make_redis_key(region, key)
            await client.delete(redis_key)
        except Exception as e:
            logger.error(f"Failed to delete key (async): {key} in region: {region}, error: {e}")

    async def clear(self, region: Optional[str] = None) -> None:
        """
        清空选定区域；未指定区域时清空当前 Redis 数据库。

        清理故障仅记录日志，调用结束不保证全部键均已删除。

        :param region: 要清空的区域；None 或空字符串会执行 flushdb，影响当前数据库的全部键
        """
        try:
            current_client = await self._connect()
            client = self.client if self.client is current_client else current_client
            if region:
                cache_region = self.__get_region(region)
                redis_key = f"{cache_region}:key:*"
                async with client.pipeline() as pipe:
                    async for key in client.scan_iter(redis_key):
                        await pipe.delete(key)
                    await pipe.execute()
                logger.debug(f"Cleared Redis cache for region (async): {region}")
            else:
                await client.flushdb()
                logger.info("Cleared all Redis cache (async)")
        except Exception as e:
            logger.error(f"Failed to clear cache (async), region: {region}, error: {e}")

    async def items(self, region: Optional[str] = None) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        扫描并逐项读取缓存，遇到故障时记录日志并结束遍历。

        扫描期间可能发生并发修改，不提供一致性快照；整个数据库遍历可能遇到非本系统缓存。

        :param region: 限定区域；None 或空字符串会扫描当前数据库的全部键
        :yields: 原始缓存键与解码后的值组成的二元组
        """
        try:
            current_client = await self._connect()
            client = self.client if self.client is current_client else current_client
            if region:
                cache_region = self.__get_region(region)
                redis_key = f"{cache_region}:key:*"
                async for key in client.scan_iter(redis_key):
                    value = await client.get(key)
                    if value is not None:
                        yield self.__get_original_key(key), deserialize(value)
            else:
                async for key in client.scan_iter("*"):
                    value = await client.get(key)
                    if value is not None:
                        yield self.__get_original_key(key), deserialize(value)
        except Exception as e:
            logger.error(f"Failed to get items from Redis, region: {region}, error: {e}")

    async def test(self) -> bool:
        """
        尝试取得可用的 Redis 客户端，连接失败时记录日志。

        :return: 客户端就绪时返回 True，否则返回 False；已缓存的客户端不会再次发送探测请求
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
        await self._close_client()
        logger.debug("Redis async connection closed")


class RedisBackend(AtomicCacheBackend):
    """
    通过同步 Redis 客户端实现缓存后端。
    """

    def __init__(self, ttl: Optional[int] = None) -> None:
        """
        初始化缓存后端并取得共享连接管理器，不立即连接 Redis。

        :param ttl: 默认有效期，单位为秒；None 表示不设置过期时间
        """
        self.ttl = ttl
        self.redis_helper = RedisHelper()

    def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **kwargs: Any,
    ) -> None:
        """
        写入缓存；非正有效期改为删除键，后端异常仅记录日志。

        :param key: 区域内的原始缓存键
        :param value: 交给缓存序列化器编码的值
        :param ttl: 有效期（秒）；None 使用实例默认值，最终仍为 None 时不过期
        :param region: 缓存区域；为空时使用 DEFAULT
        :param kwargs: 传给 Redis set 的额外选项，maxsize 会被忽略
        """
        ttl = self.ttl if ttl is None else ttl
        if ttl is not None and ttl <= 0:
            self.redis_helper.delete(key, region=region)
            return
        self.redis_helper.set(key, value, ttl=ttl, region=region, **kwargs)

    def store(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **kwargs: Any,
    ) -> None:
        """
        严格写入 Redis，供需要确定写入结果的一次性状态使用。

        非正有效期通过 GETDEL 删除原值；连接、序列化、命令错误均向调用方传播。

        :param key: 区域内的原始缓存键
        :param value: 交给缓存序列化器编码的值
        :param ttl: 有效期（秒）；None 使用实例默认值，最终仍为 None 时不过期
        :param region: 缓存区域；为空时使用 DEFAULT
        :param kwargs: 传给 Redis set 的额外选项，maxsize 会被忽略
        :raises RuntimeError: 连接建立失败，或写入未得到 True 确认
        """
        ttl = self.ttl if ttl is None else ttl
        if ttl is not None and ttl <= 0:
            self.redis_helper.consume(key, region=region)
            return
        self.redis_helper.store(key, value, ttl=ttl, region=region, **kwargs)

    def consume(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Optional[Any]:
        """
        使用 Redis 原子命令读取并删除缓存值，故障向调用方传播。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 解码后的值；键不存在时返回 None，解码失败也不会恢复已删除的值
        """
        return self.redis_helper.consume(key, region=region)

    def exists(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> bool:
        """
        判断缓存键是否存在。
        """
        return self.redis_helper.exists(key, region=region)

    def get(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Optional[Any]:
        """
        读取缓存值，查询或解码异常仅记录日志。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 解码后的值；键不存在或后端故障时返回 None
        """
        return self.redis_helper.get(key, region=region)

    def delete(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> None:
        """
        删除缓存键。
        """
        self.redis_helper.delete(key, region=region)

    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清空选定区域或当前 Redis 数据库，异常仅记录日志。

        :param region: 默认为 DEFAULT 区域；None 或空字符串会清空整个 Redis 数据库
        """
        self.redis_helper.clear(region=region)

    def items(
        self,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Generator[Tuple[str, Any], None, None]:
        """
        遍历缓存键值对，后端故障会提前结束遍历。

        :param region: 默认为 DEFAULT 区域；None 或空字符串会遍历整个 Redis 数据库
        :yields: 原始缓存键与解码后值组成的二元组
        """
        return self.redis_helper.items(region=region)

    def close(self) -> None:
        """
        释放当前缓存实例；共享的 Redis 连接池不在此关闭。

        所有 Redis 缓存共用同一个 ``RedisHelper`` 连接池，由关闭流程的「Redis缓存连接」步骤统一关闭。
        单个缓存在这里关池会断开其他仍在使用的缓存，随后又被自动重连。
        """

    @staticmethod
    def is_redis() -> bool:
        """
        标记当前后端为 Redis。
        """
        return True


class AsyncRedisBackend(AsyncCacheBackend):
    """
    通过异步 Redis 客户端实现缓存后端。
    """

    def __init__(self, ttl: Optional[int] = None) -> None:
        """
        初始化缓存后端并取得共享连接管理器，不立即连接 Redis。

        :param ttl: 默认有效期，单位为秒；None 表示不设置过期时间
        """
        self.ttl = ttl
        self.redis_helper = AsyncRedisHelper()

    async def set(  # type: ignore[override]
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **kwargs: Any,
    ) -> None:
        """
        写入缓存；非正有效期改为删除键，后端异常仅记录日志。

        :param key: 区域内的原始缓存键
        :param value: 交给缓存序列化器编码的值
        :param ttl: 有效期（秒）；None 使用实例默认值，最终仍为 None 时不过期
        :param region: 缓存区域；为空时使用 DEFAULT
        :param kwargs: 传给 Redis set 的额外选项，maxsize 会被忽略
        """
        ttl = self.ttl if ttl is None else ttl
        if ttl is not None and ttl <= 0:
            await self.redis_helper.delete(key, region=region)
            return
        await self.redis_helper.set(key, value, ttl=ttl, region=region, **kwargs)

    async def exists(  # type: ignore[override]
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> bool:
        """
        异步判断缓存键是否存在。
        """
        return await self.redis_helper.exists(key, region=region)

    async def get(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Optional[Any]:
        """
        读取缓存值，查询或解码异常仅记录日志。

        :param key: 区域内的原始缓存键
        :param region: 缓存区域；为空时使用 DEFAULT
        :return: 解码后的值；键不存在或后端故障时返回 None
        """
        return await self.redis_helper.get(key, region=region)

    async def delete(  # type: ignore[override]
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> None:
        """
        异步删除缓存键。
        """
        await self.redis_helper.delete(key, region=region)

    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:  # type: ignore[override]
        """
        清空选定区域或当前 Redis 数据库，异常仅记录日志。

        :param region: 默认为 DEFAULT 区域；None 或空字符串会清空整个 Redis 数据库
        """
        await self.redis_helper.clear(region=region)

    async def items(  # type: ignore[override]
        self,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        遍历缓存键值对，后端故障会提前结束遍历。

        :param region: 默认为 DEFAULT 区域；None 或空字符串会遍历整个 Redis 数据库
        :yields: 原始缓存键与解码后值组成的二元组
        """
        async for item in self.redis_helper.items(region=region):
            yield item

    async def close(self) -> None:  # type: ignore[override]
        """
        释放当前异步缓存实例；共享的异步 Redis 连接池由关闭流程的「异步Redis缓存连接」步骤统一关闭。
        """

    @staticmethod
    def is_redis() -> bool:
        """
        标记当前后端为 Redis。
        """
        return True
