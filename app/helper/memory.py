import gc
import psutil
import threading
import time
from typing import Optional, Callable, Any
from functools import wraps
from app.log import logger
from app.utils.singleton import Singleton


class MemoryManager(metaclass=Singleton):
    """
    内存管理工具类，用于监控和优化内存使用
    """
    
    def __init__(self):
        self._memory_threshold = 512  # 内存使用阈值(MB)
        self._check_interval = 300   # 检查间隔(秒)
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        
    @staticmethod
    def get_memory_usage() -> dict:
        """
        获取当前内存使用情况
        """
        process = psutil.Process()
        memory_info = process.memory_info()
        system_memory = psutil.virtual_memory()
        
        return {
            'rss': memory_info.rss / 1024 / 1024,  # MB
            'vms': memory_info.vms / 1024 / 1024,  # MB
            'percent': process.memory_percent(),
            'system_percent': system_memory.percent,
            'system_available': system_memory.available / 1024 / 1024 / 1024  # GB
        }
    
    def force_gc(self, generation: Optional[int] = None) -> int:
        """
        强制执行垃圾回收
        :param generation: 垃圾回收代数，None表示所有代数
        :return: 回收的对象数量
        """
        before_memory = self.get_memory_usage()
        
        if generation is not None:
            collected = gc.collect(generation)
        else:
            collected = gc.collect()
            
        after_memory = self.get_memory_usage()
        memory_freed = before_memory['rss'] - after_memory['rss']
        
        if memory_freed > 1:  # 释放超过1MB才记录
            logger.info(f"垃圾回收完成: 回收对象 {collected} 个, 释放内存 {memory_freed:.2f}MB")
            
        return collected
    
    def check_memory_and_cleanup(self) -> bool:
        """
        检查内存使用量，如果超过阈值则执行清理
        :return: 是否执行了清理
        """
        memory_info = self.get_memory_usage()
        current_memory_mb = memory_info['rss']
        
        if current_memory_mb > self._memory_threshold:
            logger.warning(f"内存使用超过阈值: {current_memory_mb:.1f}MB > {self._memory_threshold}MB, 开始清理...")
            self.force_gc()
            
            # 再次检查清理效果
            after_memory = self.get_memory_usage()
            logger.info(f"清理后内存: {after_memory['rss']:.1f}MB")
            return True
        return False
    
    def start_monitoring(self):
        """
        开始内存监控
        """
        if self._monitoring:
            return
            
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info(f"内存监控已启动 - 阈值: {self._memory_threshold}MB, 检查间隔: {self._check_interval}秒")
    
    def stop_monitoring(self):
        """
        停止内存监控
        """
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("内存监控已停止")
    
    def _monitor_loop(self):
        """
        内存监控循环
        """
        while self._monitoring:
            try:
                self.check_memory_and_cleanup()
                time.sleep(self._check_interval)
            except Exception as e:
                logger.error(f"内存监控出错: {e}")
                time.sleep(60)  # 出错后等待1分钟再继续
    
    def set_threshold(self, threshold_mb: int):
        """
        设置内存使用阈值
        :param threshold_mb: 内存阈值，单位MB（500-4096之间）
        """
        self._memory_threshold = max(512, min(4096, threshold_mb))
        logger.info(f"内存阈值已设置为: {self._memory_threshold}MB")
    
    def set_check_interval(self, interval: int):
        """
        设置检查间隔
        :param interval: 检查间隔，单位秒（最少60秒）
        """
        self._check_interval = max(60, interval)
        logger.info(f"内存检查间隔已设置为: {self._check_interval}秒")
    
    def get_threshold(self) -> int:
        """
        获取当前内存阈值
        :return: 当前阈值(MB)
        """
        return self._memory_threshold


def memory_optimized(force_gc_after: bool = False, log_memory: bool = False):
    """
    内存优化装饰器
    :param force_gc_after: 函数执行后是否强制垃圾回收
    :param log_memory: 是否记录内存使用情况
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            memory_manager = MemoryManager()
            
            if log_memory:
                before_memory = memory_manager.get_memory_usage()
                logger.debug(f"{func.__name__} 执行前内存: {before_memory['rss']:.1f}MB")
            
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                if force_gc_after:
                    memory_manager.force_gc()
                
                if log_memory:
                    after_memory = memory_manager.get_memory_usage()
                    logger.debug(f"{func.__name__} 执行后内存: {after_memory['rss']:.1f}MB")
        
        return wrapper
    return decorator


def clear_large_objects(*objects):
    """
    清理大型对象的辅助函数
    """
    for obj in objects:
        if hasattr(obj, 'clear') and callable(obj.clear):
            obj.clear()
        elif hasattr(obj, '__dict__'):
            obj.__dict__.clear()
        del obj
    gc.collect()
