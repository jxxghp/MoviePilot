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
from typing import Callable, Any, Optional

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
    if not sys.platform.startswith("linux"):
        return None
    try:
        # CDLL(None) 取当前进程的全局符号表，包含 LD_PRELOAD 进来的 jemalloc
        libc = ctypes.CDLL(None)
    except OSError:
        return None
    mallctl = getattr(libc, "mallctl", None)  # 动态符号：仅 jemalloc 提供
    if mallctl is not None:
        mallctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        mallctl.restype = ctypes.c_int
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
