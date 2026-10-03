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
    """通过本地文件系统保存二进制缓存。"""

    def __init__(self, base: Path) -> None:
        """初始化缓存根目录。"""
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    def set(
        self,
        key: str,
        value: Any,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **_kwargs,
    ) -> None:
        """原子写入一个二进制缓存文件。"""
        cache_path = self.base / region / key
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
        """判断缓存文件是否存在。"""
        return (self.base / region / key).exists()

    def get(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Optional[Any]:
        """读取二进制缓存文件。"""
        cache_path = self.base / region / key
        if not cache_path.exists():
            return None
        with cache_path.open("rb") as file_handle:
            return file_handle.read()

    def delete(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> None:
        """删除缓存文件或缓存子目录。"""
        cache_path = self.base / region / key
        if cache_path.is_file():
            cache_path.unlink()
        elif cache_path.exists():
            shutil.rmtree(cache_path, ignore_errors=True)

    def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """清空指定缓存区或缓存根目录。"""
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
        """按相对键遍历指定缓存区中的二进制文件。"""
        cache_path = self.base / region
        if not cache_path.exists():
            return
        for item in sorted(cache_path.rglob("*")):
            if item.is_file():
                with item.open("rb") as file_handle:
                    yield item.relative_to(cache_path).as_posix(), file_handle.read()

    def close(self) -> None:
        """文件缓存没有需要关闭的持久连接。"""


class AsyncFileBackend(AsyncCacheBackend):
    """通过异步文件接口保存二进制缓存。"""

    def __init__(self, base: Path) -> None:
        """初始化异步缓存根目录。"""
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    async def set(
        self,
        key: str,
        value: Any,
        region: Optional[str] = DEFAULT_CACHE_REGION,
        **_kwargs,
    ) -> None:
        """异步原子写入一个二进制缓存文件。"""
        cache_path = AsyncPath(self.base) / region / key
        await cache_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.tempfile.NamedTemporaryFile(
            dir=cache_path.parent,
            delete=False,
        ) as tmp_file:
            await tmp_file.write(value)
            temp_path = AsyncPath(tmp_file.name)
        await temp_path.replace(cache_path)

    async def exists(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> bool:
        """异步判断缓存文件是否存在。"""
        return await (AsyncPath(self.base) / region / key).exists()

    async def get(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> Optional[Any]:
        """异步读取二进制缓存文件。"""
        cache_path = AsyncPath(self.base) / region / key
        if not await cache_path.exists():
            return None
        async with aiofiles.open(cache_path, "rb") as file_handle:
            return await file_handle.read()

    async def delete(
        self,
        key: str,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> None:
        """异步删除缓存文件或缓存子目录。"""
        cache_path = AsyncPath(self.base) / region / key
        if await cache_path.is_file():
            await cache_path.unlink()
        elif await cache_path.exists():
            await aioshutil.rmtree(cache_path, ignore_errors=True)

    async def clear(self, region: Optional[str] = DEFAULT_CACHE_REGION) -> None:
        """异步清空指定缓存区或缓存根目录。"""
        cache_path = AsyncPath(self.base) / region if region else AsyncPath(self.base)
        if not await cache_path.exists():
            return
        async for item in cache_path.iterdir():
            if await item.is_file():
                await item.unlink()
            else:
                await aioshutil.rmtree(item, ignore_errors=True)

    async def items(
        self,
        region: Optional[str] = DEFAULT_CACHE_REGION,
    ) -> AsyncGenerator[Tuple[str, Any], None]:
        """异步按相对键遍历指定缓存区中的二进制文件。"""
        cache_path = AsyncPath(self.base) / region
        if not await cache_path.exists():
            return
        async for item in cache_path.rglob("*"):
            if await item.is_file():
                key = Path(str(item)).relative_to(Path(str(cache_path))).as_posix()
                async with aiofiles.open(item, "rb") as file_handle:
                    yield key, await file_handle.read()

    async def close(self) -> None:
        """异步文件缓存没有需要关闭的持久连接。"""


def configure_platform_cache(
    *,
    backend_type_provider: Callable[[], str],
    redis_factory: Optional[Callable[[Optional[int]], AtomicCacheBackend]] = None,
    async_redis_factory: Optional[Callable[[Optional[int]], AsyncCacheBackend]] = None,
    redis_probe: Optional[Callable[[], bool]] = None,
) -> None:
    """把文件适配器和启动层选定的 Redis 适配器注册到平台缓存工厂。"""
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
