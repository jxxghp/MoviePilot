"""订阅级进程内准入与显式执行上下文测试。"""

from concurrent.futures import ThreadPoolExecutor

from app.application.subscription.execution import (
    SubscriptionExecutionAdmission,
    SubscriptionExecutionContext,
)


def test_same_subscription_is_mutually_exclusive_across_channels() -> None:
    """同一订阅只能由 Search 或 Match 中的一条路径持有。"""
    admission = SubscriptionExecutionAdmission()

    search = admission.try_acquire(
        subscription_id=7,
        operation="search",
        ttl_seconds=60,
    )
    match = admission.try_acquire(
        subscription_id=7,
        operation="match",
        ttl_seconds=60,
    )

    assert search is not None
    assert match is None
    assert admission.release(search) is True
    assert admission.try_acquire(
        subscription_id=7,
        operation="match",
        ttl_seconds=60,
    ) is not None


def test_concurrent_channels_admit_only_one_owner_for_same_subscription() -> None:
    """Search 与 Match 同时申请同一订阅时只能有一个取得 owner。"""
    admission = SubscriptionExecutionAdmission()

    def acquire(operation: str):
        """并发申请固定订阅的通道所有权。"""
        return admission.try_acquire(
            subscription_id=11,
            operation=operation,
            ttl_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(acquire, ("search", "match")))

    assert sum(lease is not None for lease in leases) == 1


def test_different_subscriptions_can_run_across_channels() -> None:
    """不同订阅可以分别占用 Search 与 Match 通道。"""
    admission = SubscriptionExecutionAdmission()

    search = admission.try_acquire(
        subscription_id=7,
        operation="search",
        ttl_seconds=60,
    )
    match = admission.try_acquire(
        subscription_id=8,
        operation="match",
        ttl_seconds=60,
    )

    assert search is not None
    assert match is not None
    assert search.owner_token != match.owner_token


def test_expired_owner_keeps_ownership_until_finally_release() -> None:
    """TTL 只请求 owner 协作退出，不允许后到任务强制接管活跃执行。"""
    now = [100.0]
    admission = SubscriptionExecutionAdmission(clock=lambda: now[0])
    lease = admission.try_acquire(
        subscription_id=9,
        operation="search",
        ttl_seconds=5,
    )
    assert lease is not None

    now[0] = 106.0

    assert admission.is_expired(lease) is True
    assert admission.try_acquire(
        subscription_id=9,
        operation="match",
        ttl_seconds=5,
    ) is None
    assert admission.release(lease) is True

    replacement = admission.try_acquire(
        subscription_id=9,
        operation="match",
        ttl_seconds=5,
    )
    assert replacement is not None
    assert admission.release(lease) is False


def test_execution_context_combines_cancel_ttl_and_download_boundary() -> None:
    """执行上下文独立承载取消、TTL、阶段与下载副作用状态。"""
    now = [100.0]
    cancelled = [False]
    phases: list[tuple[str, int | None]] = []
    admission = SubscriptionExecutionAdmission(clock=lambda: now[0])
    lease = admission.try_acquire(
        subscription_id=10,
        operation="search",
        ttl_seconds=5,
    )
    assert lease is not None
    context = SubscriptionExecutionContext(
        lease=lease,
        admission=admission,
        task_id="task-10",
        cancel_requested=lambda: cancelled[0],
        phase_changed=lambda phase, site_id: phases.append((phase, site_id)),
    )

    assert context.should_stop() is False
    context.report_phase("searching", 3)
    context.mark_download_started()

    assert context.download_started is True
    assert phases == [("searching", 3), ("submitting", None)]

    cancelled[0] = True
    assert context.is_cancel_requested() is True
    assert context.should_stop() is True

    cancelled[0] = False
    now[0] = 106.0
    assert context.is_expired() is True
    assert context.should_stop() is True
