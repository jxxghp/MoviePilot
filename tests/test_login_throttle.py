"""登录失败指数退避限流器的策略测试。"""

from app.application.security.throttle import LoginThrottle, build_login_throttle_key, get_login_throttle


class _Clock:
    """可手动推进的单调时钟。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """推进时钟。"""
        self.now += seconds


def _throttle(clock: _Clock, **overrides) -> LoginThrottle:
    """构造使用假时钟的限流器。"""
    params = {
        "max_failures": 5,
        "base_lock_seconds": 30,
        "max_lock_seconds": 900,
        "ttl_seconds": 3600,
        "max_items": 4096,
        "clock": clock,
    }
    params.update(overrides)
    return LoginThrottle(**params)


KEY = ("10.0.0.1", "admin")


def test_key_normalizes_username_and_tolerates_missing_host() -> None:
    """用户名去空白并小写，客户端地址缺失时用空串。"""
    assert build_login_throttle_key("10.0.0.1", "  Admin ") == ("10.0.0.1", "admin")
    assert build_login_throttle_key(None, None) == ("", "")


def test_failures_below_threshold_do_not_lock() -> None:
    """未达到阈值前不锁定。"""
    throttle = _throttle(_Clock())
    for _ in range(4):
        assert throttle.record_failure(KEY) == 0
    assert throttle.retry_after(KEY) == 0


def test_reaching_threshold_locks_and_doubles_until_capped() -> None:
    """第 5 次失败锁 30 秒，之后每次翻倍直到 900 秒上限。"""
    clock = _Clock()
    throttle = _throttle(clock)
    for _ in range(4):
        throttle.record_failure(KEY)

    assert throttle.record_failure(KEY) == 30
    assert throttle.retry_after(KEY) == 30

    expected = [60, 120, 240, 480, 900, 900]
    for lock_seconds in expected:
        assert throttle.record_failure(KEY) == lock_seconds
        assert throttle.retry_after(KEY) == lock_seconds


def test_retry_after_counts_down_and_releases() -> None:
    """锁定期内返回剩余秒数（向上取整），到期后放行。"""
    clock = _Clock()
    throttle = _throttle(clock)
    for _ in range(5):
        throttle.record_failure(KEY)

    clock.advance(10.2)
    assert throttle.retry_after(KEY) == 20
    clock.advance(20)
    assert throttle.retry_after(KEY) == 0


def test_success_clears_failure_history() -> None:
    """登录成功后计数清零，再失败从头计数。"""
    throttle = _throttle(_Clock())
    for _ in range(5):
        throttle.record_failure(KEY)

    throttle.record_success(KEY)

    assert throttle.retry_after(KEY) == 0
    assert throttle.record_failure(KEY) == 0


def test_stale_entries_expire_after_ttl() -> None:
    """超过 TTL 且不在锁定期的记录被清理，再失败从头计数。"""
    clock = _Clock()
    throttle = _throttle(clock, ttl_seconds=60)
    for _ in range(3):
        throttle.record_failure(KEY)

    clock.advance(61)
    throttle.record_failure(("10.0.0.2", "other"))

    assert throttle.record_failure(KEY) == 0


def test_capacity_evicts_least_recently_updated() -> None:
    """超过容量时淘汰最久未更新的键。"""
    clock = _Clock()
    throttle = _throttle(clock, max_items=2)
    throttle.record_failure(("1.1.1.1", "a"))
    clock.advance(1)
    throttle.record_failure(("2.2.2.2", "b"))
    clock.advance(1)
    throttle.record_failure(("3.3.3.3", "c"))

    assert ("1.1.1.1", "a") not in throttle._entries  # pylint: disable=protected-access
    assert ("2.2.2.2", "b") in throttle._entries  # pylint: disable=protected-access
    assert ("3.3.3.3", "c") in throttle._entries  # pylint: disable=protected-access


def test_keys_are_isolated_by_client_and_username() -> None:
    """不同客户端或不同用户名互不影响。"""
    throttle = _throttle(_Clock())
    for _ in range(5):
        throttle.record_failure(KEY)

    assert throttle.retry_after(("10.0.0.2", "admin")) == 0
    assert throttle.retry_after(("10.0.0.1", "other")) == 0


def test_default_throttle_is_shared() -> None:
    """进程内默认实例唯一。"""
    assert get_login_throttle() is get_login_throttle()
