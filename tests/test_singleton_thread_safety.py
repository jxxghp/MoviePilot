import threading

from app.foundation.singleton import Singleton, SingletonClass


def _construct_concurrently(singleton_type, count: int = 16):
    """让多个线程同时越过起跑线，放大首次构造竞态。"""
    barrier = threading.Barrier(count)
    instances = []

    def construct() -> None:
        barrier.wait()
        instances.append(singleton_type())

    threads = [threading.Thread(target=construct) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    return instances


def test_parameterized_singleton_first_construction_is_single_flight():
    """同一参数的并发首次构造只能发布一个完整实例。"""
    class _ParameterizedSingleton(metaclass=Singleton):
        pass

    instances = _construct_concurrently(_ParameterizedSingleton)

    assert len({id(instance) for instance in instances}) == 1


def test_class_singleton_first_construction_is_single_flight():
    """按类单例的并发首次构造只能发布一个完整实例。"""
    class _ClassSingleton(metaclass=SingletonClass):
        pass

    instances = _construct_concurrently(_ClassSingleton)

    assert len({id(instance) for instance in instances}) == 1
