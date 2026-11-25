"""
验证单例实例获取和重复构造行为。
"""

import pytest

from app.foundation.singleton import Singleton, SingletonClass, WeakSingleton


def test_singleton_class_can_read_existing_instance_without_creating(monkeypatch):
    """
    按类单例可以只读取已存在实例
    """

    class Example(metaclass=SingletonClass):
        """
        只在显式构造后产生可查询实例的按类单例。
        """

    monkeypatch.setattr(SingletonClass, "_instances", {})

    assert Example.get_existing_instance() is None
    instance = Example()
    assert Example.get_existing_instance() is instance


def test_parameterized_singleton_can_read_matching_instance_without_creating(monkeypatch):
    """
    参数化单例按相同参数读取已存在实例
    """

    class Example(metaclass=Singleton):
        """
        按名称区分实例的参数化单例。
        """

        def __init__(self, name):
            """
            保存用于区分实例的构造参数。
            """
            self.name = name

    monkeypatch.setattr(Singleton, "_instances", {})

    assert Example.get_existing_instance("first") is None
    instance = Example("first")
    assert Example.get_existing_instance("first") is instance
    assert Example.get_existing_instance("second") is None


def test_parameterized_singleton_releases_only_matching_owner(monkeypatch):
    """
    迟到的旧 owner 不得释放同类当前已发布的新 owner。
    """

    class Example(metaclass=Singleton):
        """
        用于验证按参数单例 owner 身份。
        """

    monkeypatch.setattr(Singleton, "_instances", {})
    current = Example()

    assert Example.release_existing_instance(object()) is False
    assert Example.get_existing_instance() is current
    assert Example.release_existing_instance(current) is True
    assert Example.get_existing_instance() is None


def test_class_singleton_releases_only_matching_owner(monkeypatch):
    """
    按类单例仅允许当前 owner 释放自身身份。
    """

    class Example(metaclass=SingletonClass):
        """
        用于验证按类单例 owner 身份。
        """

    monkeypatch.setattr(SingletonClass, "_instances", {})
    current = Example()

    assert Example.release_existing_instance(object()) is False
    assert Example.get_existing_instance() is current
    assert Example.release_existing_instance(current) is True
    assert Example.get_existing_instance() is None


def test_weak_singleton_reads_and_releases_only_current_owner(monkeypatch):
    """
    弱单例应支持不触发构造的读取，并只释放当前 owner。
    """

    class Example(metaclass=WeakSingleton):
        """
        用于验证弱单例 owner 身份。
        """

    monkeypatch.setattr(WeakSingleton, "_instances", {})

    assert Example.get_existing_instance() is None
    current = Example()
    assert Example.get_existing_instance() is current
    assert Example.release_existing_instance(object()) is False
    assert Example.release_existing_instance(current) is True
    assert Example.get_existing_instance() is None


def test_parameterized_singleton_can_retain_failed_lifecycle_owner(monkeypatch):
    """
    构造中途失败时，可选 owner 单例必须仍能由启动失败清理读取。
    """

    class Example(metaclass=Singleton):
        """
        模拟在构造期已创建后台 owner 后抛错的参数化单例。
        """

        _retain_failed_singleton = True

        def __init__(self) -> None:
            """
            发布可观察 owner 后模拟后续启动失败。
            """
            self.owner_started = True
            raise RuntimeError("startup failed")

    monkeypatch.setattr(Singleton, "_instances", {})

    with pytest.raises(RuntimeError, match="startup failed"):
        Example()

    retained = Example.get_existing_instance()
    assert retained is not None
    assert retained.owner_started is True


def test_class_singleton_can_retain_failed_lifecycle_owner(monkeypatch):
    """
    按类 owner 单例也必须在 __init__ 抛错后保留可清理身份。
    """

    class Example(metaclass=SingletonClass):
        """
        模拟目录监控构造中途失败的按类单例。
        """

        _retain_failed_singleton = True

        def __init__(self) -> None:
            """
            发布可观察 owner 后模拟后续启动失败。
            """
            self.owner_started = True
            raise RuntimeError("startup failed")

    monkeypatch.setattr(SingletonClass, "_instances", {})

    with pytest.raises(RuntimeError, match="startup failed"):
        Example()

    retained = Example.get_existing_instance()
    assert retained is not None
    assert retained.owner_started is True


@pytest.mark.parametrize("metaclass", [Singleton, SingletonClass])
@pytest.mark.parametrize("retain_failed", [False, True])
def test_singleton_forwards_constructor_arguments_once(monkeypatch, metaclass, retain_failed):
    """
    普通与提前发布路径均应按原顺序向构造方法传递位置和关键字参数。
    """
    calls = []
    monkeypatch.setattr(metaclass, "_instances", {})

    class Example(metaclass=metaclass):
        """
        记录自定义构造方法及初始化方法收到的参数。
        """

        _retain_failed_singleton = retain_failed

        def __new__(cls, name, *, enabled):
            """
            记录参数并创建当前类的实例。
            """
            calls.append(("new", name, enabled))
            return object.__new__(cls)

        def __init__(self, name, *, enabled):
            """
            记录参数并检查需要保留的 owner 已提前发布。
            """
            calls.append(("init", name, enabled))
            if retain_failed:
                if metaclass is Singleton:
                    assert Example.get_existing_instance(name, enabled=enabled) is self
                else:
                    assert Example.get_existing_instance() is self

    instance = Example("owner", enabled=True)
    assert Example("owner", enabled=True) is instance
    assert calls == [("new", "owner", True), ("init", "owner", True)]


@pytest.mark.parametrize("metaclass", [Singleton, SingletonClass])
@pytest.mark.parametrize("retain_failed", [False, True])
def test_singleton_preserves_foreign_new_result(monkeypatch, metaclass, retain_failed):
    """
    自定义 __new__ 返回其它类型时跳过初始化，并保留原有缓存差异。
    """
    result = object()
    calls = []
    monkeypatch.setattr(metaclass, "_instances", {})

    class Example(metaclass=metaclass):
        """
        模拟返回异类对象的动态构造器。
        """

        _retain_failed_singleton = retain_failed

        def __new__(cls):
            """
            返回外部对象，验证元类没有强制转换或重建实例。
            """
            calls.append("new")
            return result

        def __init__(self):
            """
            异类构造结果不应进入本类初始化方法。
            """
            raise AssertionError("foreign instance must not be initialized")

    assert Example() is result
    assert Example.get_existing_instance() is (None if retain_failed else result)
    assert Example() is result
    assert calls == ["new"] * (2 if retain_failed else 1)
