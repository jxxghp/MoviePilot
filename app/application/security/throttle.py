"""密码登录的连续失败计数与指数退避锁定策略。"""

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

# 连续失败达到该次数后开始锁定
LOGIN_MAX_FAILURES = 5
# 首次锁定时长，之后每再失败一次翻倍
LOGIN_BASE_LOCK_SECONDS = 30
# 单次锁定上限
LOGIN_MAX_LOCK_SECONDS = 15 * 60
# 记录在最后一次失败后保留多久
LOGIN_THROTTLE_TTL_SECONDS = 60 * 60
# 内存记录上限，超出后按最近更新时间淘汰最旧
LOGIN_THROTTLE_MAX_ITEMS = 4096
# 指数上限，避免 2 ** n 无意义增长
_MAX_BACKOFF_EXPONENT = 20

LoginThrottleKey = tuple[str, str]


def build_login_throttle_key(client_host: Optional[str], username: Optional[str]) -> LoginThrottleKey:
    """
    构造登录限流键：(客户端地址, 归一化用户名)。

    只使用直连客户端地址，不解析 X-Forwarded-For；没有可信代理配置时盲信该头会让
    攻击者换头绕过。同机反代场景下所有请求同地址，键退化为按用户名限流，这正是
    暴力破解防护需要的维度；直接暴露服务时复合键避免单一地址扫号锁死所有用户。
    """
    return str(client_host or "").strip(), str(username or "").strip().lower()


@dataclass
class _Entry:
    """单个键的失败计数与锁定状态。"""

    failures: int = 0
    locked_until: float = 0.0
    updated_at: float = 0.0


class LoginThrottle:
    """
    按 (客户端地址, 用户名) 记录连续失败并施加指数锁定的进程内限流器。

    连续失败达到 ``max_failures`` 后锁定 ``base_lock_seconds``，之后每再失败一次
    锁定时长翻倍直到 ``max_lock_seconds``；登录成功清除记录。状态只在当前进程
    内存中，多 worker 进程之间不共享。
    """

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        base_lock_seconds: int = LOGIN_BASE_LOCK_SECONDS,
        max_lock_seconds: int = LOGIN_MAX_LOCK_SECONDS,
        ttl_seconds: int = LOGIN_THROTTLE_TTL_SECONDS,
        max_items: int = LOGIN_THROTTLE_MAX_ITEMS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max(1, int(max_failures))
        self._base_lock_seconds = max(1, int(base_lock_seconds))
        self._max_lock_seconds = max(self._base_lock_seconds, int(max_lock_seconds))
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_items = max(1, int(max_items))
        self._clock = clock
        self._entries: dict[LoginThrottleKey, _Entry] = {}
        self._lock = threading.RLock()

    def retry_after(self, key: LoginThrottleKey) -> int:
        """返回该键仍需等待的秒数（向上取整），0 表示允许尝试。"""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return 0
            remaining = entry.locked_until - now
            return math.ceil(remaining) if remaining > 0 else 0

    def record_failure(self, key: LoginThrottleKey) -> int:
        """记录一次失败，返回本次施加的锁定秒数，0 表示尚未达到锁定阈值。"""
        now = self._clock()
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            entry.failures += 1
            entry.updated_at = now
            lock_seconds = 0
            if entry.failures >= self._max_failures:
                exponent = min(entry.failures - self._max_failures, _MAX_BACKOFF_EXPONENT)
                lock_seconds = min(self._base_lock_seconds * (2 ** exponent), self._max_lock_seconds)
                entry.locked_until = now + lock_seconds
            self._cleanup(now)
            return lock_seconds

    def record_success(self, key: LoginThrottleKey) -> None:
        """登录成功后清除该键的失败记录。"""
        with self._lock:
            self._entries.pop(key, None)

    def _cleanup(self, now: float) -> None:
        """清理过期记录，并在超出容量时淘汰最久未更新的记录。"""
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.locked_until <= now and now - entry.updated_at > self._ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
        overflow = len(self._entries) - self._max_items
        if overflow <= 0:
            return
        ordered = sorted(self._entries.items(), key=lambda item: item[1].updated_at)
        for key, _ in ordered[:overflow]:
            self._entries.pop(key, None)


_default_throttle = LoginThrottle()


def get_login_throttle() -> LoginThrottle:
    """返回进程内共享的登录限流器。"""
    return _default_throttle
