"""订阅兜底搜索的站点级容量、间隔与冷却治理。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from app.application.site.observation import SiteSearchObservation
from app.runtime.stop import StopState


class SubscriptionSearchCancelled(RuntimeError):
    """表示订阅搜索在可取消预算等待点终止。"""


@dataclass(frozen=True, slots=True)
class SubscriptionSiteBudgetDeferral:
    """记录一次站点预算冲突及该站点最早可再次尝试的时间。"""

    site_id: int
    retry_at: str


class SubscriptionSearchDeferred(RuntimeError):
    """表示订阅搜索未失败，而是应在站点预算可用后重新入队。"""

    def __init__(self, *, retry_at: str, site_ids: tuple[int, ...]) -> None:
        """保存队列恢复所需的时间和冲突站点，避免把临时冲突写成错误。"""
        super().__init__(f"订阅搜索已延后，站点预算最早可重试：{retry_at}")
        self.retry_at = retry_at
        self.site_ids = site_ids


class SubscriptionSiteBudgetUnavailable(RuntimeError):
    """表示站点预算暂时不可用，调用方应记录为延后而非失败。"""

    def __init__(self, *, site_id: int, retry_at: str) -> None:
        """保存站点和下一次可尝试时间，供订阅队列恢复。"""
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


@dataclass(frozen=True, slots=True)
class SubscriptionSiteBudgetMetricsSnapshot:
    """一次订阅搜索轮次内的站点预算观测快照。"""

    site_count: int
    request_count: int
    candidate_count: int
    failure_count: int
    cooldown_skip_count: int
    release_failure_count: int
    cooldown_seconds: float


class SubscriptionSiteBudgetMetrics:
    """线程安全聚合站点请求、失败、冷却与租约收口结果。"""

    def __init__(self) -> None:
        """初始化一次搜索轮次共享的计数器。"""
        self._lock = threading.Lock()
        self._site_ids: set[int] = set()
        self._request_count = 0
        self._candidate_count = 0
        self._failure_count = 0
        self._cooldown_skip_count = 0
        self._release_failure_count = 0
        self._cooldown_seconds = 0.0

    def record_unavailable(self, site_id: int) -> None:
        """记录因在途租约或错误冷却而未发出的站点请求。"""
        with self._lock:
            self._site_ids.add(site_id)
            self._cooldown_skip_count += 1

    def record_request(self, site_id: int, candidate_count: int) -> None:
        """记录一个真实站点请求及其返回候选数。"""
        with self._lock:
            self._site_ids.add(site_id)
            self._request_count += 1
            self._candidate_count += max(0, candidate_count)

    def record_finish(self, *, outcome: str, delay: float, released: bool) -> None:
        """记录外站结果、错误冷却时间和租约释放结果。"""
        with self._lock:
            if outcome not in {"success", "skipped"}:
                self._failure_count += 1
                self._cooldown_seconds += max(0.0, delay)
            if not released:
                self._release_failure_count += 1

    def record_release_failure(self) -> None:
        """记录未进入正常结果收口的站点租约释放失败。"""
        with self._lock:
            self._release_failure_count += 1

    def snapshot(self) -> SubscriptionSiteBudgetMetricsSnapshot:
        """返回可安全跨线程读取的不可变观测结果。"""
        with self._lock:
            return SubscriptionSiteBudgetMetricsSnapshot(
                site_count=len(self._site_ids),
                request_count=self._request_count,
                candidate_count=self._candidate_count,
                failure_count=self._failure_count,
                cooldown_skip_count=self._cooldown_skip_count,
                release_failure_count=self._release_failure_count,
                cooldown_seconds=round(self._cooldown_seconds, 3),
            )


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
    """以非阻塞认领保护站点租约，并持久化外站错误冷却。"""

    def __init__(
        self,
        *,
        repository: SubscriptionSiteBudgetRepository,
        owner: str,
        cancelled: Callable[[], bool],
        stop_state: StopState,
        lease_seconds: int = 900,
        clock: Callable[[], datetime] = _utc_now,
        phase_changed: Optional[Callable[[str, Optional[int]], None]] = None,
        metrics: Optional[SubscriptionSiteBudgetMetrics] = None,
    ) -> None:
        """保存持久化端口及可注入的时钟和阶段回调。"""
        self._repository = repository
        self._owner = owner
        self._cancelled = cancelled
        self._stop_state = stop_state
        self._lease_seconds = max(1, lease_seconds)
        self._clock = clock
        self._phase_changed = phase_changed
        self._metrics = metrics

    def acquire(self, site_id: int) -> SiteBudgetClaim:
        """只认领一次指定站点，未就绪时留待下一次正常调度。"""
        self._raise_if_cancelled()
        claim = self._repository.claim_site(
            site_id=site_id,
            owner=self._owner,
            lease_seconds=self._lease_seconds,
        )
        if claim.acquired:
            self._report_phase("searching", site_id)
            return claim
        if self._metrics:
            self._metrics.record_unavailable(site_id)
        self._report_phase("waiting_site_budget", site_id)
        raise SubscriptionSiteBudgetUnavailable(
            site_id=site_id,
            retry_at=claim.retry_at,
        )

    def _report_phase(self, phase: str, site_id: Optional[int]) -> None:
        """向任务所有者报告不改变预算语义的业务阶段。"""
        if self._phase_changed:
            self._phase_changed(phase, site_id)

    def record_request(self, site_id: int, candidate_count: int) -> None:
        """把真实请求及返回候选数写入轮次聚合器。"""
        if self._metrics:
            self._metrics.record_request(site_id, candidate_count)

    def record_release_failure(self) -> None:
        """把站点租约释放异常写入轮次聚合器。"""
        if self._metrics:
            self._metrics.record_release_failure()

    def finish(self, claim: SiteBudgetClaim, observation: SiteSearchObservation) -> bool:
        """正常结果立即释放站点，错误结果按类别写入冷却。"""
        if not claim.lease_token:
            self.record_release_failure()
            return False
        outcome = observation.outcome if observation.attempted else "skipped"
        delay = self._next_delay(outcome, claim.consecutive_failures)
        next_allowed_at = (self._clock() + timedelta(seconds=delay)).isoformat(timespec="seconds")
        released = self._repository.finish_site(
            site_id=claim.site_id,
            lease_token=claim.lease_token,
            outcome=outcome,
            next_allowed_at=next_allowed_at,
            error=observation.error,
        )
        if self._metrics:
            self._metrics.record_finish(
                outcome=outcome,
                delay=delay,
                released=released,
            )
        return released

    def _next_delay(self, outcome: str, consecutive_failures: int) -> float:
        """正常或本地跳过立即恢复，错误按连续失败次数退避。"""
        if outcome in {"success", "skipped"}:
            return 0.0
        exponent = min(max(consecutive_failures, 0), 5)
        base, ceiling = {
            "rate_limited": (900.0, 21600.0),
            "forbidden": (900.0, 21600.0),
            "login_invalid": (900.0, 21600.0),
            "timeout": (300.0, 7200.0),
        }.get(outcome, (180.0, 3600.0))
        return float(min(base * (2**exponent), ceiling))

    def _raise_if_cancelled(self) -> None:
        """在创建站点租约前传播取消或停机。"""
        if self._stop_state.is_system_stopped or self._cancelled():
            raise SubscriptionSearchCancelled("订阅搜索已取消")
