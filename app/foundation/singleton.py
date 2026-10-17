"""
提供按类和构造参数复用实例的单例机制。
"""

import abc
import threading
import weakref
from typing import Any, Callable, Optional, TypeVar, cast

_ExistingInstance = TypeVar("_ExistingInstance")

_InstanceKey = tuple[type[object], tuple[object, ...], frozenset[tuple[str, object]]]


class Singleton(abc.ABCMeta, type):
    """
    类单例模式（按参数）。
    """

    _instances: dict[_InstanceKey, object] = {}
    _lock = threading.RLock()

    def get_existing_instance(cls, *args: Any, **kwargs: Any) -> Any:
        """
        按相同参数返回已创建实例，不触发初始化。
        """
        key = (cls, args, frozenset(kwargs.items()))
        with cls._lock:
            return cls._instances.get(key)

    def release_existing_instance(cls, instance: object) -> bool:
        """
        仅在身份匹配时释放已收敛实例，供显式生命周期重新创建实例。
        """
        with cls._lock:
            keys = [
                key
                for key, value in cls._instances.items()
                if key[0] is cls and value is instance
            ]
            for key in keys:
                cls._instances.pop(key, None)
            return bool(keys)

    def __call__(cls, *args: object, **kwargs: object) -> object:
        """
        按类和构造参数创建或复用实例。
        """
        key = (cls, args, frozenset(kwargs.items()))
        with cls._lock:
            if key not in cls._instances:
                if getattr(cls, "_retain_failed_singleton", False):
                    # 启动线程的生命周期管理对象 必须先发布身份再执行 __init__；
                    # 构造中途抛错时保留实例，启动失败清理才能找到已创建的管理对象。
                    instance_type: type[object] = cls
                    construct: Callable[..., object] = instance_type.__new__
                    instance = construct(cls, *args, **kwargs)
                    if not isinstance(instance, cls):
                        return instance
                    cls._instances[key] = instance
                    initialize: Callable[..., None] = instance_type.__init__
                    initialize(instance, *args, **kwargs)
                else:
                    cls._instances[key] = super().__call__(*args, **kwargs)
            return cls._instances[key]


class AbstractSingleton(abc.ABC, metaclass=Singleton):
    """
    抽象类单例模式。
    """
    pass


class SingletonClass(abc.ABCMeta, type):
    """
    类单例模式（按类）。
    """

    _instances: dict[type[object], object] = {}
    _lock = threading.RLock()

    def get_existing_instance(cls: type[_ExistingInstance]) -> Optional[_ExistingInstance]:
        """
        返回与类键对应的已创建实例，不触发初始化。
        """
        # cls 是本元类的实例；缓存按该类键保存其构造结果。
        singleton_class = cast(SingletonClass, cls)
        with singleton_class._lock:
            return cast(Optional[_ExistingInstance], singleton_class._instances.get(cls))

    def release_existing_instance(cls, instance: object) -> bool:
        """
        仅在身份匹配时释放已收敛实例，供显式生命周期重新创建实例。
        """
        with cls._lock:
            if cls._instances.get(cls) is not instance:
                return False
            cls._instances.pop(cls, None)
            return True

    def __call__(cls, *args: object, **kwargs: object) -> object:
        """
        按类创建或复用唯一实例。
        """
        with cls._lock:
            if cls not in cls._instances:
                if getattr(cls, "_retain_failed_singleton", False):
                    # 与参数化单例保持相同的实例发布顺序；锁会阻止其他线程
                    # 在 __init__ 返回或抛错前读取半构造实例。
                    instance_type: type[object] = cls
                    construct: Callable[..., object] = instance_type.__new__
                    instance = construct(cls, *args, **kwargs)
                    if not isinstance(instance, cls):
                        return instance
                    cls._instances[cls] = instance
                    initialize: Callable[..., None] = instance_type.__init__
                    initialize(instance, *args, **kwargs)
                else:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
            return cls._instances[cls]


class AbstractSingletonClass(abc.ABC, metaclass=SingletonClass):
    """
    抽象类单例模式（按类）。
    """
    pass


class WeakSingleton(abc.ABCMeta, type):
    """
    弱引用单例模式 - 当没有强引用时自动清理。
    """
    _instances: weakref.WeakKeyDictionary[type[object], object] = weakref.WeakKeyDictionary()
    _lock = threading.RLock()

    def get_existing_instance(cls) -> Any:
        """
        返回仍存活的已创建实例，不触发初始化。
        """
        with cls._lock:
            return cls._instances.get(cls)

    def release_existing_instance(cls, instance: object) -> bool:
        """
        仅在身份匹配时释放已关闭实例。
        """
        with cls._lock:
            if cls._instances.get(cls) is not instance:
                return False
            cls._instances.pop(cls, None)
            return True

    def __call__(cls, *args: object, **kwargs: object) -> object:
        """
        按类创建或复用仍有强引用的实例。
        """
        with cls._lock:
            if cls not in cls._instances:
                cls._instances[cls] = super().__call__(*args, **kwargs)
            return cls._instances[cls]
