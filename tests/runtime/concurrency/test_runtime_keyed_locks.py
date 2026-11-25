"""
进程内按键互斥锁的行为回归。
"""

import threading

import pytest

from app.runtime.locks import KeyedLocks


def test_same_key_is_serialized_case_insensitively():
    """
    同一键（不区分大小写，下载器返回的 hash 大小写不一）在持有期间不能被另一个线程拿到。
    """
    locks = KeyedLocks()
    entered = threading.Event()
    release = threading.Event()
    acquired_while_held = []

    def holder():
        """
        持有小写键直到测试放行。
        """
        with locks.hold(["abcdef"]):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    entered.wait(5)
    stripe = locks._locks[locks._stripe("ABCDEF")]
    acquired_while_held.append(stripe.acquire(blocking=False))
    release.set()
    thread.join(5)

    assert acquired_while_held == [False]
    assert not stripe.locked()


def test_multiple_keys_are_released_even_on_error():
    """
    一次持有多个键（同一种子的 v1/v2 hash）时全部加锁，异常退出也全部释放；空键集合不加锁。
    """
    locks = KeyedLocks(stripes=4)

    with pytest.raises(RuntimeError):
        with locks.hold(["a", "b", "c", ""]):
            assert all(lock.locked() for lock in {locks._locks[locks._stripe(key)] for key in "abc"})
            raise RuntimeError("boom")

    assert not any(lock.locked() for lock in locks._locks)
    with locks.hold([]):
        assert not any(lock.locked() for lock in locks._locks)


def test_invalid_stripe_count_is_rejected():
    """
    分段数量必须为正数。
    """
    with pytest.raises(ValueError):
        KeyedLocks(stripes=0)


def test_same_thread_can_reenter_the_same_key():
    """
    同一线程持有期间再次进入同一键不会自己卡死（例如持锁提交下载时又调用了同样持锁的暂停接口），
    退出最外层后才真正释放。
    """
    locks = KeyedLocks()
    stripe = locks._locks[locks._stripe("abc")]

    with locks.hold(["abc"]):
        with locks.hold(["ABC"]):
            assert stripe.locked()
        assert stripe.locked()

    assert not stripe.locked()
