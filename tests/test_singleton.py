import pytest

from app.foundation.singleton import Singleton, SingletonClass


def test_singleton_class_can_read_existing_instance_without_creating(monkeypatch):
    """按类单例可以只读取已存在实例"""

    class Example(metaclass=SingletonClass):
        pass

    monkeypatch.setattr(SingletonClass, "_instances", {})

    assert Example.get_existing_instance() is None
    instance = Example()
    assert Example.get_existing_instance() is instance


def test_parameterized_singleton_can_read_matching_instance_without_creating(monkeypatch):
    """参数化单例按相同参数读取已存在实例"""

    class Example(metaclass=Singleton):
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(Singleton, "_instances", {})

    assert Example.get_existing_instance("first") is None
    instance = Example("first")
    assert Example.get_existing_instance("first") is instance
    assert Example.get_existing_instance("second") is None


def test_parameterized_singleton_can_retain_failed_lifecycle_owner(monkeypatch):
    """构造中途失败时，可选 owner 单例必须仍能由启动失败清理读取。"""

    class Example(metaclass=Singleton):
        """模拟在构造期已创建后台 owner 后抛错的参数化单例。"""

        _retain_failed_singleton = True

        def __init__(self) -> None:
            """发布可观察 owner 后模拟后续启动失败。"""
            self.owner_started = True
            raise RuntimeError("startup failed")

    monkeypatch.setattr(Singleton, "_instances", {})

    with pytest.raises(RuntimeError, match="startup failed"):
        Example()

    retained = Example.get_existing_instance()
    assert retained is not None
    assert retained.owner_started is True


def test_class_singleton_can_retain_failed_lifecycle_owner(monkeypatch):
    """按类 owner 单例也必须在 __init__ 抛错后保留可清理身份。"""

    class Example(metaclass=SingletonClass):
        """模拟目录监控构造中途失败的按类单例。"""

        _retain_failed_singleton = True

        def __init__(self) -> None:
            """发布可观察 owner 后模拟后续启动失败。"""
            self.owner_started = True
            raise RuntimeError("startup failed")

    monkeypatch.setattr(SingletonClass, "_instances", {})

    with pytest.raises(RuntimeError, match="startup failed"):
        Example()

    retained = Example.get_existing_instance()
    assert retained is not None
    assert retained.owner_started is True
