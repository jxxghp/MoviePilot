from app.utils.singleton import Singleton, SingletonClass


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
