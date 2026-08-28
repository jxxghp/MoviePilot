"""Scheduler ExecutionRegistry 的状态所有权与原子操作测试。"""

import asyncio
import concurrent.futures

from app.scheduler.registry import ExecutionRegistry


def test_registry_assigns_monotonic_generations() -> None:
    """同 ID generation 单调递增，不同任务分别计数。"""
    registry = ExecutionRegistry()
    state: dict[str, object] = {}

    assert registry.assign_generation("job", state) == 1
    assert state["_generation"] == 1
    assert registry.next_generation("job") == 2
    assert registry.next_generation("other") == 1
    assert registry.current_generation("job") == 2
    assert registry.current_generation("missing") == 0


def test_registry_allocates_unique_generations_across_threads() -> None:
    """并发分配同 ID 时每个调用都必须取得唯一 generation。"""
    registry = ExecutionRegistry()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        generations = list(
            executor.map(lambda _index: registry.next_generation("job"), range(64))
        )

    assert sorted(generations) == list(range(1, 65))


def test_registry_claims_and_releases_one_active_generation() -> None:
    """旧 generation 活跃期间拒绝同 ID 新 generation，并精确释放 owner。"""
    registry = ExecutionRegistry()

    assert registry.claim_generation("job", 1) is True
    assert registry.claim_generation("job", 2) is False
    assert registry.is_active("job") is True
    assert registry.active_generations("job") == frozenset({1})
    assert registry.release_generation("job", 2) is False
    assert registry.release_generation("job", 1) is True
    assert registry.is_active("job") is False


def test_registry_reservation_preserves_owner_identity() -> None:
    """预约只能由原 owner 消费或释放，且活跃任务不能再次预约。"""
    registry = ExecutionRegistry()

    assert registry.reserve("job", owner=11) is True
    assert registry.reserve("job", owner=12) is False
    assert registry.reservation_owner("job") == 11
    assert registry.consume_reservation("job", owner=12) is False
    assert registry.release_reservation("job", owner=12) is False
    assert registry.consume_reservation("job", owner=11) is True
    assert registry.consume_reservation("job", owner=12) is True

    assert registry.claim_generation("job", 1) is True
    assert registry.reserve("job", owner=11) is False


def test_registry_filters_and_removes_handles_by_completion_identity() -> None:
    """句柄查询按 owner 字段过滤，摘除键使用真实完成信号身份。"""
    registry = ExecutionRegistry()
    loop = asyncio.new_event_loop()
    try:
        submitted: concurrent.futures.Future[object] = concurrent.futures.Future()
        completed: concurrent.futures.Future[object] = concurrent.futures.Future()
        progress: concurrent.futures.Future[object] = concurrent.futures.Future()
        job_handle = registry.register_handle(
            job_id="job",
            generation=1,
            loop=loop,
            handle=submitted,
            completion=completed,
        )
        progress_handle = registry.register_handle(
            job_id="job",
            generation=1,
            loop=loop,
            handle=progress,
            kind="progress",
        )

        assert registry.handles(job_id="job", generation=1) == (
            job_handle,
            progress_handle,
        )
        assert registry.handles(job_id="job", kind="progress") == (
            progress_handle,
        )
        assert registry.remove_handle(submitted) is False
        assert registry.remove_handle(completed) is True
        assert registry.handles() == (progress_handle,)
    finally:
        loop.close()


def test_registry_stop_snapshot_clears_reservations_but_retains_handles() -> None:
    """停止快照封存当前句柄并清理尚未消费的手动预约。"""
    registry = ExecutionRegistry()
    loop = asyncio.new_event_loop()
    try:
        completion: concurrent.futures.Future[object] = concurrent.futures.Future()
        handle = registry.register_handle(
            job_id="job",
            generation=1,
            loop=loop,
            handle=completion,
        )
        assert registry.reserve("manual", owner=11) is True

        assert registry.stop_snapshot() == (handle,)
        assert registry.reservation_owner("manual") is None
        assert registry.handles() == (handle,)
        assert registry.remove_handle(completion) is True
        assert registry.handles() == ()
    finally:
        loop.close()
