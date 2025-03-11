import abc


class Singleton(abc.ABCMeta, type):
    """
    类单例模式（按参数）
    """

    _instances: dict = {}

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

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(SingletonClass, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class AbstractSingletonClass(abc.ABC, metaclass=SingletonClass):
    """
    抽像类单例模式（按类）
    """
    pass
