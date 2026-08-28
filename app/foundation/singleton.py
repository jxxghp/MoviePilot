import abc
import threading
import weakref


class Singleton(abc.ABCMeta, type):
    """
    类单例模式（按参数）
    """

    _instances: dict = {}
    _lock = threading.RLock()

    def get_existing_instance(cls, *args, **kwargs):
        """按相同参数返回已创建实例，不触发初始化"""
        key = (cls, args, frozenset(kwargs.items()))
        with cls._lock:
            return cls._instances.get(key)

    def release_existing_instance(cls, instance: object) -> bool:
        """仅在身份匹配时释放已收敛实例，供显式生命周期重新创建 owner。"""
        with cls._lock:
            keys = [
                key
                for key, value in cls._instances.items()
                if key[0] is cls and value is instance
            ]
            for key in keys:
                cls._instances.pop(key, None)
            return bool(keys)

    def __call__(cls, *args, **kwargs):
        """按类和构造参数创建或复用实例。"""
        key = (cls, args, frozenset(kwargs.items()))
        with cls._lock:
            if key not in cls._instances:
                if getattr(cls, "_retain_failed_singleton", False):
                    # 启动线程的 lifecycle owner 必须先发布身份再执行 __init__；
                    # 构造中途抛错时保留实例，启动失败清理才能找到已创建的 owner。
                    instance = cls.__new__(cls, *args, **kwargs)
                    if not isinstance(instance, cls):
                        return instance
                    cls._instances[key] = instance
                    cls.__init__(instance, *args, **kwargs)
                else:
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
    _lock = threading.RLock()

    def get_existing_instance(cls):
        """返回已创建实例，不触发初始化"""
        with cls._lock:
            return cls._instances.get(cls)

    def release_existing_instance(cls, instance: object) -> bool:
        """仅在身份匹配时释放已收敛实例，供显式生命周期重新创建 owner。"""
        with cls._lock:
            if cls._instances.get(cls) is not instance:
                return False
            cls._instances.pop(cls, None)
            return True

    def __call__(cls, *args, **kwargs):
        """按类创建或复用唯一实例。"""
        with cls._lock:
            if cls not in cls._instances:
                if getattr(cls, "_retain_failed_singleton", False):
                    # 与参数化单例保持相同的 owner 发布顺序；锁会阻止其他线程
                    # 在 __init__ 返回或抛错前读取半构造实例。
                    instance = cls.__new__(cls, *args, **kwargs)
                    if not isinstance(instance, cls):
                        return instance
                    cls._instances[cls] = instance
                    cls.__init__(instance, *args, **kwargs)
                else:
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
        """按类创建或复用仍有强引用的实例。"""
        with cls._lock:
            if cls not in cls._instances:
                cls._instances[cls] = super().__call__(*args, **kwargs)
            return cls._instances[cls]
