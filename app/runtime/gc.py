"""
内存回收装饰器模块
提供装饰器用于在函数执行后立即回收内存
"""
import ctypes
import gc
import functools
import logging
import psutil
import os
import sys
from typing import Callable, Any, Optional, cast

logger = logging.getLogger(__name__)


def memory_gc(force_collect: bool = True,
              log_memory_usage: bool = False) -> Callable:
    """
    内存回收装饰器

    Args:
        force_collect: 是否强制执行垃圾回收，默认True
        log_memory_usage: 是否记录内存使用日志，默认False

    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # 记录函数执行前的内存使用情况
            memory_before = None
            memory_after = None
            if log_memory_usage:
                memory_before = get_memory_usage()
                logger.info(f"函数 {func.__name__} 执行前内存使用: {memory_before}")

            try:
                # 执行原函数
                result = func(*args, **kwargs)

                # 记录函数执行后的内存使用情况
                if log_memory_usage:
                    memory_after = get_memory_usage()
                    logger.info(f"函数 {func.__name__} 执行后内存使用: {memory_after}")
                    if memory_before:
                        memory_diff = memory_after - memory_before
                        logger.info(f"函数 {func.__name__} 内存变化: {memory_diff} MB")

                return result

            finally:
                # 强制垃圾回收
                if force_collect:
                    collected = gc.collect()
                    if log_memory_usage:
                        logger.info(f"函数 {func.__name__} 垃圾回收完成，回收对象数: {collected}")

                # 记录垃圾回收后的内存使用情况
                if log_memory_usage:
                    memory_after_gc = get_memory_usage()
                    logger.info(f"函数 {func.__name__} 垃圾回收后内存使用: {memory_after_gc}")
                    if memory_after:
                        memory_freed = memory_after - memory_after_gc
                        logger.info(f"函数 {func.__name__} 释放内存: {memory_freed} MB")

        return wrapper
    return decorator


def get_memory_usage() -> float:
    """
    获取当前进程的内存使用情况（MB）

    Returns:
        内存使用量（MB）
    """
    try:
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        return memory_info.rss / 1024 / 1024  # 转换为MB
    except Exception as e:
        logger.warning(f"获取内存使用情况失败: {e}")
        return 0.0


# jemalloc 的 MALLCTL_ARENAS_ALL：对全部 arena 执行 purge
_JEMALLOC_ARENAS_ALL_PURGE = b"arena.4096.purge"
# 把调用线程的线程缓存（tcache）交还所属 arena
_JEMALLOC_THREAD_TCACHE_FLUSH = b"thread.tcache.flush"
# 后台回收线程开关与数量上限，均可在运行时写入
_JEMALLOC_BACKGROUND_THREAD = b"background_thread"
_JEMALLOC_MAX_BACKGROUND_THREADS = b"max_background_threads"
# MALLOC_CONF 中与后台线程相关、不能被子进程继承的配置项
_BACKGROUND_THREAD_OPTIONS = frozenset({"background_thread", "max_background_threads"})


@functools.cache
def _process_libc() -> Optional[ctypes.CDLL]:
    """返回当前进程的全局符号表；非 Linux 或无法加载时返回 None。"""
    if not sys.platform.startswith("linux"):
        return None
    try:
        # CDLL(None) 取当前进程的全局符号表，包含 LD_PRELOAD 进来的 jemalloc
        return ctypes.CDLL(None)
    except OSError:
        return None


@functools.cache
def _jemalloc_mallctl() -> Optional[Callable[..., int]]:
    """返回 jemalloc 的 ``mallctl``；进程未加载 jemalloc 时返回 None。只解析一次，供高频调用复用。"""
    libc = _process_libc()
    if libc is None:
        return None
    mallctl = getattr(libc, "mallctl", None)  # 动态符号：仅 jemalloc 提供
    if mallctl is None:
        return None
    mallctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    mallctl.restype = ctypes.c_int
    return cast(Callable[..., int], mallctl)


def flush_thread_allocator_cache() -> bool:
    """
    把调用线程在 jemalloc 线程缓存中囤积的空闲块交还 arena。

    jemalloc 只在线程自身继续分配时才回收它的线程缓存，线程池里空闲等待的 worker
    不再分配，最后一个任务留下的缓存会一直计入 RSS（线程越多越明显）。交还后的空闲页
    再由 arena 的衰减机制归还系统。只能由持有缓存的线程自己调用；未使用 jemalloc 时不做任何事。

    Returns:
        是否实际执行了刷新
    """
    mallctl = _jemalloc_mallctl()
    if mallctl is None:
        return False
    return mallctl(_JEMALLOC_THREAD_TCACHE_FLUSH, None, None, None, 0) == 0


def configure_allocator_background_thread(max_threads: int = 1) -> bool:
    """
    让 jemalloc 后台回收线程只在主进程运行，并从子进程将继承的 ``MALLOC_CONF`` 中移除相关项。

    后台线程按衰减时间把空闲脏页归还系统，但 ``MALLOC_CONF`` 与 ``LD_PRELOAD`` 会被所有子进程
    继承，Chromium 的 GPU 进程在后台线程开启时无法启动，浏览器仿真整体 abort。

    Args:
        max_threads: 后台线程上限

    Returns:
        是否在运行时实际开启了后台线程
    """
    configured = _strip_background_thread_conf()
    if configured:
        return False
    mallctl = _jemalloc_mallctl()
    if mallctl is None:
        return False
    limit = ctypes.c_size_t(max_threads)
    if mallctl(_JEMALLOC_MAX_BACKGROUND_THREADS, None, None, ctypes.byref(limit), ctypes.sizeof(limit)) != 0:
        return False
    enabled = ctypes.c_bool(True)
    return mallctl(_JEMALLOC_BACKGROUND_THREAD, None, None, ctypes.byref(enabled), ctypes.sizeof(enabled)) == 0


def _strip_background_thread_conf() -> bool:
    """从 ``os.environ["MALLOC_CONF"]`` 去掉后台线程项，返回原配置是否包含这些项。"""
    conf = os.environ.get("MALLOC_CONF")
    if not conf:
        return False
    options = [option for option in conf.split(",") if option]
    kept = [option for option in options if option.split(":", 1)[0] not in _BACKGROUND_THREAD_OPTIONS]
    if len(kept) == len(options):
        return False
    if kept:
        os.environ["MALLOC_CONF"] = ",".join(kept)
    else:
        del os.environ["MALLOC_CONF"]
    return True


def release_allocator_memory() -> Optional[str]:
    """
    把 C 分配器中已释放但尚未归还的空闲页交还操作系统。

    ``gc.collect()`` 只把 Python 对象交还分配器，jemalloc 默认没有后台回收线程，空闲 arena
    的脏页会一直计入 RSS，glibc 的堆顶碎片同理。官方镜像通过 ``LD_PRELOAD`` 加载 jemalloc 时
    调用 ``mallctl`` purge；源码部署等使用 glibc 的环境调用 ``malloc_trim(0)``。两者都只归还
    空闲页、不影响存活对象，其他平台或符号不存在时不做任何事。

    Returns:
        实际使用的机制（``jemalloc`` 或 ``glibc``），未执行时返回 None
    """
    libc = _process_libc()
    if libc is None:
        return None
    mallctl = _jemalloc_mallctl()
    if mallctl is not None:
        if mallctl(_JEMALLOC_ARENAS_ALL_PURGE, None, None, None, 0) == 0:
            return "jemalloc"
        return None
    malloc_trim = getattr(libc, "malloc_trim", None)  # 动态符号：仅 glibc 提供
    if malloc_trim is not None:
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        malloc_trim(0)
        return "glibc"
    return None


def memory_monitor(threshold_mb: Optional[float] = None) -> Callable:
    """
    内存监控装饰器，当内存使用超过阈值时自动触发垃圾回收

    Args:
        threshold_mb: 内存阈值（MB），超过此值将触发垃圾回收

    Returns:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # 检查内存使用情况
            current_memory = get_memory_usage()

            if threshold_mb and current_memory > threshold_mb:
                logger.warning(f"内存使用超过阈值 {threshold_mb}MB，当前使用: {current_memory}MB")
                collected = gc.collect()
                logger.info(f"自动垃圾回收完成，回收对象数: {collected}")

            # 执行原函数
            result = func(*args, **kwargs)

            # 执行后再次检查并回收
            if threshold_mb:
                memory_after = get_memory_usage()
                if memory_after > threshold_mb:
                    collected = gc.collect()
                    logger.info(f"函数执行后垃圾回收完成，回收对象数: {collected}")

            return result

        return wrapper
    return decorator


# 便捷的装饰器别名
memory_cleanup = memory_gc
auto_gc = memory_gc(force_collect=True, log_memory_usage=True)
memory_watch = memory_monitor
