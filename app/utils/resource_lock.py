# -*- coding: utf-8 -*-
import threading
from collections import defaultdict

from app.log import logger
from app.utils.singleton import Singleton


class ResourceLockManager(metaclass=Singleton):
    """基于 resource_id 的锁管理器"""
    def __init__(self):
        self.locks = defaultdict(threading.Lock)  # 每个资源一个锁
        self.global_lock = threading.RLock()  # 管理全局锁池的线程安全

    def get_lock(self, resource_id):
        """获取与 resource_id 关联的锁"""
        with self.global_lock:  # 防止并发修改 locks
            return self.locks[resource_id]

    def clean_lock(self, resource_id):
        """尝试清理锁"""
        with self.global_lock:  # 防止并发修改 locks
            lock = self.locks.get(resource_id)
            if not lock:
                return
            if not lock.locked():
                del self.locks[resource_id]
                logger.debug(f"线程 {threading.current_thread().name} 清理了未占用的锁 {resource_id}")
            else:
                logger.debug(f"线程 {threading.current_thread().name} 无法清理仍被占用的锁 {resource_id}")


class ResourceLockHandler:
    """资源处理器，用于封装对单个 resource_id 的锁管理"""
    def __init__(self, resource_id, blocking=True):
        """
        :param resource_id: 要处理的资源 ID
        :param blocking: 是否阻塞等待获取锁
        """
        self.resource_id = resource_id
        self.manager = ResourceLockManager()
        self.lock = None
        self.blocking = blocking
        self.acquired = False

    def __enter__(self):
        """自动获取锁"""
        self.lock = self.manager.get_lock(self.resource_id)
        self.acquired = self.lock.acquire(blocking=self.blocking)
        if self.acquired:
            logger.debug(f"线程 {threading.current_thread().name} 获取了锁 {self.resource_id}")
        else:
            logger.debug(f"线程 {threading.current_thread().name} 无法获取锁 {self.resource_id}")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """自动释放锁并清理"""
        if self.acquired:  # 仅当成功获取锁时才释放和清理
            self.lock.release()
            self.lock = None
            self.manager.clean_lock(self.resource_id)
