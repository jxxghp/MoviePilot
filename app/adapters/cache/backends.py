"""
文件缓存后端及平台缓存工厂装配，不在本模块实现内存缓存策略。
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Generator, Optional, Tuple

import aiofiles
import aioshutil
from anyio import Path as AsyncPath

from app.runtime.cache import (
    DEFAULT_CACHE_REGION,
    AsyncCacheBackend,
    AtomicCacheBackend,
    CacheBackend,
    configure_cache_factories,
)
from app.runtime.settings import get_runtime_setting


class FileBackend(CacheBackend):
    """
    以区域子目录和相对键定位二进制缓存文件。

    调用方负责提供受信任的区域和相对键；后端不校验路径越界，也不自行管理过期时间。
    文件读写错误向调用方传播，目录删除沿用忽略错误的清理行为。
    """

    def __init__(self, base: Path) -> None:
        """
        初始化缓存根目录；目录不存在时同步创建。

        :param base: 此后读写缓存使用的根路径
        """
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **_kwargs: object,
    ) -> None:
        """
        通过同目录临时文件替换目标，避免读者看到未写完的内容。

        :param key: 区域内的相对缓存文件路径
        :param value: 可写入二进制文件的字节数据；本层不序列化对象
        :param ttl: 新式TTL关键字，文件后端当前不管理过期；字符串第三位置参数沿用旧region合同
        :param region: 根目录下的缓存区域名称，使用时须为字符串
        :param _kwargs: 为缓存接口兼容保留，文件后端不处理这些选项
        """
        cache_region = ttl if isinstance(ttl, str) else (region or DEFAULT_CACHE_REGION)
        cache_path = self.base / cache_region / key
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=cache_path.parent,
            delete=False,
        ) as tmp_file:
            tmp_file.write(value)
            temp_path = Path(tmp_file.name)
        temp_path.replace(cache_path)

    def exists(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> bool:
        """
        检查缓存路径是否存在，不区分文件与目录。

        :param key: 区域内的相对缓存路径
        :param region: 根目录下的缓存区域名称，使用时须为字符串
        :return: 路径存在时返回 True
        """
        cache_region = region or DEFAULT_CACHE_REGION
        return (self.base / cache_region / key).exists()

    def get(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Optional[Any]:
        """
        读取缓存文件的完整二进制内容。

        :param key: 区域内的相对缓存文件路径
        :param region: 根目录下的缓存区域名称，使用时须为字符串
        :return: 文件内容；检查时目标不存在则返回 None
        """
        cache_region = region or DEFAULT_CACHE_REGION
        cache_path = self.base / cache_region / key
        if not cache_path.exists():
            return None
        with cache_path.open("rb") as file_handle:
            return file_handle.read()

    def delete(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> None:
        """
        删除指定缓存文件或递归清理缓存子目录。

        :param key: 区域内的相对缓存路径；不存在时不操作
        :param region: 根目录下的缓存区域名称，使用时须为字符串
        """
        cache_region = region or DEFAULT_CACHE_REGION
        cache_path = self.base / cache_region / key
        if cache_path.is_file():
            cache_path.unlink()
        elif cache_path.exists():
            shutil.rmtree(cache_path, ignore_errors=True)

    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """
        清理缓存目录的内容，保留区域目录本身。

        :param region: 要清空的区域；为空时清空整个缓存根目录
        """
        cache_path = self.base / region if region else self.base
        if not cache_path.exists():
            return
        for item in cache_path.iterdir():
            if item.is_file():
                item.unlink()
            else:
                shutil.rmtree(item, ignore_errors=True)

    def items(
        self,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Generator[Tuple[str, Any], None, None]:
        """
        递归读取区域中的缓存文件，不提供并发修改时的一致性快照。

        :param region: 要遍历的区域名称，使用时须为字符串
        :yields: 相对于区域目录的路径与完整文件字节组成的二元组
        """
        cache_path = self.base / region if region else self.base
        if not cache_path.exists():
            return
        for item in sorted(cache_path.rglob("*")):
            if item.is_file():
                with item.open("rb") as file_handle:
                    yield item.relative_to(cache_path).as_posix(), file_handle.read()

    def close(self) -> None:
        """
        文件缓存没有需要关闭的持久连接。
        """


class AsyncFileBackend(AsyncCacheBackend):
    """
    以区域子目录和相对键定位二进制缓存文件。

    调用方负责提供受信任的区域和相对键；后端不校验路径越界，也不自行管理过期时间。
    文件读写错误向调用方传播，目录删除沿用忽略错误的清理行为。
    """

    def __init__(self, base: Path) -> None:
        """
        初始化缓存根目录；目录不存在时同步创建。

        :param base: 此后读写缓存使用的根路径
        """
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    async def set(  # type: ignore[override]
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **_kwargs: object,
    ) -> None:
        """
        通过同目录临时文件替换目标，避免读者看到未写完的内容。

        :param key: 区域内的相对缓存文件路径
        :param value: 可写入二进制文件的字节数据；本层不序列化对象
        :param ttl: 新式TTL关键字，文件后端当前不管理过期；字符串第三位置参数沿用旧region合同
        :param region: 根目录下的缓存区域名称，使用时须为字符串
        :param _kwargs: 为缓存接口兼容保留，文件后端不处理这些选项
        """
        cache_region = ttl if isinstance(ttl, str) else (region or DEFAULT_CACHE_REGION)
        cache_path = AsyncPath(self.base) / cache_region / key
        await cache_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.tempfile.NamedTemporaryFile(
            dir=cache_path.parent,
            delete=False,
        ) as tmp_file:
            await tmp_file.write(value)
            temp_path = AsyncPath(tmp_file.name)
        await temp_path.replace(cache_path)

    async def exists(  # type: ignore[override]
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> bool:
        """
        检查缓存路径是否存在，不区分文件与目录。

        :param key: 区域内的相对缓存路径
        :param region: 根目录下的缓存区域名称，使用时须为字符串
        :return: 路径存在时返回 True
        """
        cache_region = region or DEFAULT_CACHE_REGION
        return bool(await (AsyncPath(self.base) / cache_region / key).exists())

    async def get(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Optional[Any]:
        """
        读取缓存文件的完整二进制内容。

        :param key: 区域内的相对缓存文件路径
        :param region: 根目录下的缓存区域名称，使用时须为字符串
        :return: 文件内容；检查时目标不存在则返回 None
        """
        cache_region = region or DEFAULT_CACHE_REGION
        cache_path = AsyncPath(self.base) / cache_region / key
        if not await cache_path.exists():
            return None
        async with aiofiles.open(cache_path, "rb") as file_handle:
            return await file_handle.read()

    async def delete(  # type: ignore[override]
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> None:
        """
        删除指定缓存文件或递归清理缓存子目录。

        :param key: 区域内的相对缓存路径；不存在时不操作
        :param region: 根目录下的缓存区域名称，使用时须为字符串
        """
        cache_region = region or DEFAULT_CACHE_REGION
        cache_path = AsyncPath(self.base) / cache_region / key
        if await cache_path.is_file():
            await cache_path.unlink()
        elif await cache_path.exists():
            await aioshutil.rmtree(cache_path, ignore_errors=True)

    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:  # type: ignore[override]
        """
        清理缓存目录的内容，保留区域目录本身。

        :param region: 要清空的区域；为空时清空整个缓存根目录
        """
        cache_path = AsyncPath(self.base) / region if region else AsyncPath(self.base)
        if not await cache_path.exists():
            return
        async for item in cache_path.iterdir():
            if await item.is_file():
                await item.unlink()
            else:
                await aioshutil.rmtree(item, ignore_errors=True)

    async def items(  # type: ignore[override]
        self,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> AsyncGenerator[Tuple[str, Any], None]:
        """
        递归读取区域中的缓存文件，不提供并发修改时的一致性快照。

        :param region: 要遍历的区域名称，使用时须为字符串
        :yields: 相对于区域目录的路径与完整文件字节组成的二元组
        """
        cache_region = region or DEFAULT_CACHE_REGION
        cache_path = AsyncPath(self.base) / cache_region
        if not await cache_path.exists():
            return
        async for item in cache_path.rglob("*"):
            if await item.is_file():
                key = Path(str(item)).relative_to(Path(str(cache_path))).as_posix()
                async with aiofiles.open(item, "rb") as file_handle:
                    yield key, await file_handle.read()

    async def close(self) -> None:  # type: ignore[override]
        """
        异步文件缓存没有需要关闭的持久连接。
        """


def configure_platform_cache(
    *,
    backend_type_provider: Callable[[], str],
    redis_factory: Optional[Callable[[Optional[int]], AtomicCacheBackend]] = None,
    async_redis_factory: Optional[Callable[[Optional[int]], AsyncCacheBackend]] = None,
    redis_probe: Optional[Callable[[], bool]] = None,
) -> None:
    """
    把文件后端与启动层选定的 Redis 后端注册到运行时缓存工厂。

    文件缓存的默认根目录与清理周期从运行时设置读取；本函数只注册工厂，不连接 Redis。

    :param backend_type_provider: 返回当前缓存后端类型的配置回调
    :param redis_factory: 同步 Redis 后端工厂，接收可选默认有效期（秒）
    :param async_redis_factory: 异步 Redis 后端工厂，接收可选默认有效期（秒）
    :param redis_probe: Redis 连通性探测回调；未装配 Redis 时可省略
    """
    configure_cache_factories(
        backend_type_provider=backend_type_provider,
        redis_factory=redis_factory,
        async_redis_factory=async_redis_factory,
        redis_probe=redis_probe,
        file_factory=lambda base: FileBackend(
            base=base or get_runtime_setting('TEMP_PATH')
        ),
        async_file_factory=lambda base: AsyncFileBackend(
            base=base or get_runtime_setting('TEMP_PATH')
        ),
        file_ttl_provider=lambda: get_runtime_setting('TEMP_FILE_DAYS') * 24 * 3600,
    )
