import abc
import threading
import weakref


class Singleton(abc.ABCMeta, type):
    """
    类单例模式（按参数）
    """

    _instances: dict = {}

    def get_existing_instance(cls, *args, **kwargs):
        """按相同参数返回已创建实例，不触发初始化"""
        key = (cls, args, frozenset(kwargs.items()))
        return cls._instances.get(key)

    def __call__(cls, *args, **kwargs):
        key = (cls, args, frozenset(kwargs.items()))
        if key not in cls._instances:
            cls._instances[key] = super().__call__(*args, **kwargs)
        return cls._instances[key]


class AbstractSingleton(abc.ABC, metaclass=Singleton):
    """
    抽像类单例模式
    """
    pass


class SingletonClass(abc.ABCMeta, type):
    """
    类单例模式（按类）
    """

    _instances: dict = {}

    def get_existing_instance(cls):
        """返回已创建实例，不触发初始化"""
        return cls._instances.get(cls)

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class AbstractSingletonClass(abc.ABC, metaclass=SingletonClass):
    """
    抽像类单例模式（按类）
    """
    pass


class WeakSingleton(abc.ABCMeta, type):
    """
    弱引用单例模式 - 当没有强引用时自动清理
    """
    _instances: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
    _lock = threading.RLock()

    def __call__(cls, *args, **kwargs):
        with cls._lock:
            if cls not in cls._instances:
                cls._instances[cls] = super().__call__(*args, **kwargs)
            return cls._instances[cls]
