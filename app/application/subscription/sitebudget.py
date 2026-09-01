"""订阅兜底搜索的站点级容量、间隔与冷却治理。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from app.application.site.search_observation import SiteSearchObservation
from app.runtime.stop import StopState


class SubscriptionSearchCancelled(RuntimeError):
    """表示订阅搜索在可取消预算等待点终止。"""


class SubscriptionSiteBudgetUnavailable(RuntimeError):
    """表示站点预算超出本任务允许的短等待窗口。"""

    def __init__(self, *, site_id: int, retry_at: str) -> None:
        """保存站点和下一次可尝试时间，供批次聚合失败展示。"""
        super().__init__(f"站点 {site_id} 冷却或已有在途搜索，最早可重试：{retry_at}")
        self.site_id = site_id
        self.retry_at = retry_at


@dataclass(frozen=True, slots=True)
class SiteBudgetClaim:
    """一次站点预算认领结果或下一次可尝试时间。"""

    site_id: int
    acquired: bool
    retry_at: str
    consecutive_failures: int
    lease_token: Optional[str] = None


class SubscriptionSiteBudgetRepository(Protocol):
    """站点预算租约和冷却状态的持久化端口。"""

    def claim_site(
        self,
        *,
        site_id: int,
        owner: str,
        lease_seconds: int,
    ) -> SiteBudgetClaim:
        """认领站点唯一在途租约，未就绪时返回重试时间。"""
        ...

    def finish_site(
        self,
        *,
        site_id: int,
        lease_token: str,
        outcome: str,
        next_allowed_at: str,
        error: Optional[str] = None,
    ) -> bool:
        """收口当前站点租约并持久化下次允许时间。"""
        ...


def _utc_now() -> datetime:
    """返回带时区的 UTC 当前时间。"""
    return datetime.now(timezone.utc)


class SubscriptionSiteBudget:
    """在同步搜索 worker 内执行可取消的站点预算等待与反馈。"""

    def __init__(
        self,
        *,
        repository: SubscriptionSiteBudgetRepository,
        owner: str,
        cancelled: Callable[[], bool],
        stop_state: StopState,
        interval_range: tuple[float, float] = (60.0, 300.0),
        lease_seconds: int = 900,
        max_wait_seconds: float = 5.0,
        random_uniform: Callable[[float, float], float] = random.uniform,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utc_now,
        phase_changed: Optional[Callable[[str, Optional[int]], None]] = None,
    ) -> None:
        """保存持久化端口及可注入的时钟、随机数和等待实现。"""
        self._repository = repository
        self._owner = owner
        self._cancelled = cancelled
        self._stop_state = stop_state
        self._interval_range = interval_range
        self._lease_seconds = max(1, lease_seconds)
        self._max_wait_seconds = max(0.0, max_wait_seconds)
        self._random_uniform = random_uniform
        self._sleeper = sleeper
        self._clock = clock
        self._phase_changed = phase_changed

    def acquire(self, site_id: int) -> SiteBudgetClaim:
        """循环认领指定站点，并在每秒边界检查取消与停机。"""
        deadline = time.monotonic() + self._max_wait_seconds
        while True:
            self._raise_if_cancelled()
            claim = self._repository.claim_site(
                site_id=site_id,
                owner=self._owner,
                lease_seconds=self._lease_seconds,
            )
            if claim.acquired:
                self._report_phase("searching", site_id)
                return claim
            self._report_phase("waiting_site_budget", site_id)
            retry_at = datetime.fromisoformat(claim.retry_at)
            remaining = max(0.0, (retry_at - self._clock()).total_seconds())
            if remaining > max(0.0, deadline - time.monotonic()):
                raise SubscriptionSiteBudgetUnavailable(
                    site_id=site_id,
                    retry_at=claim.retry_at,
                )
            self._sleeper(min(max(remaining, 0.05), 1.0))

    def _report_phase(self, phase: str, site_id: Optional[int]) -> None:
        """向任务所有者报告不改变预算语义的业务阶段。"""
        if self._phase_changed:
            self._phase_changed(phase, site_id)

    def finish(self, claim: SiteBudgetClaim, observation: SiteSearchObservation) -> bool:
        """依据调用结果计算随机间隔或错误冷却并释放租约。"""
        if not claim.lease_token:
            return False
        outcome = observation.outcome if observation.attempted else "skipped"
        delay = self._next_delay(outcome, claim.consecutive_failures)
        next_allowed_at = (self._clock() + timedelta(seconds=delay)).isoformat(timespec="seconds")
        return self._repository.finish_site(
            site_id=claim.site_id,
            lease_token=claim.lease_token,
            outcome=outcome,
            next_allowed_at=next_allowed_at,
            error=observation.error,
        )

    def _next_delay(self, outcome: str, consecutive_failures: int) -> float:
        """为成功渐进恢复、错误退避和本地跳过计算下一次等待。"""
        if outcome == "skipped":
            return 0.0
        if outcome == "success":
            low, high = self._interval_range
            recovery_factor = 1.0 + min(max(consecutive_failures, 0), 3) * 0.5
            return self._random_uniform(low, high) * recovery_factor
        exponent = min(max(consecutive_failures, 0), 5)
        base, ceiling = {
            "rate_limited": (900.0, 21600.0),
            "forbidden": (900.0, 21600.0),
            "login_invalid": (900.0, 21600.0),
            "timeout": (300.0, 7200.0),
        }.get(outcome, (180.0, 3600.0))
        return float(min(base * (2**exponent), ceiling))

    def _raise_if_cancelled(self) -> None:
        """在不持有业务锁的等待边界传播取消或停机。"""
        if self._stop_state.is_system_stopped or self._cancelled():
            raise SubscriptionSearchCancelled("订阅搜索已取消")
