"""订阅搜索队列接入、锁隔离和批次失败治理测试。"""

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.execution import SubscriptionExecutionAdmission
from app.chain.subscribe.facade import SubscribeChain
from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository
from app.db.base import Base
from app.schemas.types import MediaType


class _ForbiddenLock:
    """正式队列路径若仍访问 Match 全局锁则立即失败。"""

    def acquire(self, **_kwargs):
        """禁止搜索队列取得历史长锁。"""
        raise AssertionError("持久搜索队列不应取得 Match 全局锁")

    def release(self):
        """禁止搜索队列释放从未取得的历史长锁。"""
        raise AssertionError("持久搜索队列不应释放 Match 全局锁")


class _SubscriptionRepository:
    """为搜索队列返回稳定的多订阅快照。"""

    def __init__(self, subscribes: list[SubscriptionSnapshot]) -> None:
        self._subscribes = {subscribe.id: subscribe for subscribe in subscribes}

    def list(self, _state: str = None) -> list[SubscriptionSnapshot]:
        """返回全部测试订阅。"""
        return list(self._subscribes.values())

    def get(self, subscribe_id: int) -> SubscriptionSnapshot | None:
        """按 ID 返回任务执行时重新读取的订阅。"""
        return self._subscribes.get(subscribe_id)


def _subscribe(subscribe_id: int) -> SubscriptionSnapshot:
    """构造越过新增保护期的活动电影订阅。"""
    return SubscriptionSnapshot(
        id=subscribe_id,
        name=f"治理电影 {subscribe_id}",
        year="2026",
        type=MediaType.MOVIE.value,
        media_source="themoviedb",
        media_id=str(1000 + subscribe_id),
        state="R",
        date=(datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _chain(tmp_path, subscribes: list[SubscriptionSnapshot]):
    """构造注入持久队列且不初始化其他 Chain 依赖的搜索实例。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'search-governance.db'}")
    Base.metadata.create_all(engine)
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = _SubscriptionRepository(subscribes)
    chain.subscription_search_repository = TransactionalSubscriptionSearchRepository(
        sessionmaker(bind=engine)
    )
    chain.get_states_for_search = lambda state: state
    chain._match_lock = _ForbiddenLock()
    chain._search_queue_lock = threading.Lock()
    chain._subscription_execution_admission = SubscriptionExecutionAdmission()
    return chain


def test_fallback_queue_executes_without_match_global_lock(tmp_path, monkeypatch):
    """R/P 兜底搜索在持久队列中执行，不受日常 Match 长锁阻塞。"""
    subscribes = [_subscribe(1), _subscribe(2)]
    chain = _chain(tmp_path, subscribes)
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    processed = []

    def process(subscribe, _searchchain, *, execution_context):
        """记录每条任务独立的订阅执行上下文。"""
        processed.append((subscribe.id, execution_context))
        return subscribe

    monkeypatch.setattr(
        chain,
        "_process_search_subscription",
        process,
    )

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert [subscribe_id for subscribe_id, _context in processed] == [1, 2]
    assert [context.lease.subscription_id for _subscribe_id, context in processed] == [1, 2]
    assert len({context.task_id for _subscribe_id, context in processed}) == 2
    assert len({id(context) for _subscribe_id, context in processed}) == 2
    assert batch.state == "completed"
    assert batch.finished_count == 2
    assert batch.failed_count == 0


def test_fallback_queue_continues_after_one_subscription_failure(tmp_path, monkeypatch):
    """单订阅异常不得中止批次后续任务，聚合终态必须暴露失败。"""
    subscribes = [_subscribe(3), _subscribe(4)]
    chain = _chain(tmp_path, subscribes)
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    processed = []

    def process(subscribe, _searchchain, **_kwargs):
        """让首条失败并保持第二条正常完成。"""
        processed.append(subscribe.id)
        if subscribe.id == 3:
            raise RuntimeError("provider timeout")
        return replace(subscribe)

    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert processed == [3, 4]
    assert batch.state == "failed"
    assert batch.finished_count == 1
    assert batch.failed_count == 1
    assert batch.last_error == "provider timeout"

    for subscribe_id in (3, 4):
        lease = chain._subscription_execution_admission.try_acquire(
            subscription_id=subscribe_id,
            operation="match",
            ttl_seconds=60,
        )
        assert lease is not None
        assert chain._subscription_execution_admission.release(lease) is True


def test_same_subscription_conflict_is_skipped_without_waiting(tmp_path, monkeypatch):
    """Match 已持有同一订阅时，Search 本轮完成为跳过且不进入业务处理。"""
    subscribe = _subscribe(6)
    chain = _chain(tmp_path, [subscribe])
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    process = Mock(side_effect=AssertionError("冲突订阅不应进入 Search"))
    monkeypatch.setattr(chain, "_process_search_subscription", process)
    match_lease = chain._subscription_execution_admission.try_acquire(
        subscription_id=subscribe.id,
        operation="match",
        ttl_seconds=60,
    )
    assert match_lease is not None

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    process.assert_not_called()
    assert batch.state == "skipped"
    assert batch.finished_count == 0
    assert batch.skipped_count == 1
    assert batch.last_error == "同一订阅正在由其他通道处理，本轮搜索已跳过"
    assert chain._subscription_execution_admission.release(match_lease) is True


def test_paused_subscription_is_skipped_after_admission_refresh(tmp_path, monkeypatch):
    """准入后重新读取到暂停订阅时不得开始搜索或伪造完成。"""
    subscribe = _subscribe(9)
    paused = replace(subscribe, state="S")
    chain = _chain(tmp_path, [subscribe])
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    chain.subscription_repository.get = Mock(side_effect=(subscribe, paused))
    process = Mock(side_effect=AssertionError("暂停订阅不应进入 Search"))
    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    process.assert_not_called()
    assert batch.state == "skipped"
    assert batch.finished_count == 0
    assert batch.skipped_count == 1
    assert batch.last_error == "订阅已暂停，本轮搜索已跳过"


def test_cleanup_failures_cannot_leak_subscription_admission(tmp_path, monkeypatch):
    """站点预算和状态清理都失败时仍必须释放当前订阅 owner。"""
    subscribe = replace(_subscribe(7), state="N")
    chain = _chain(tmp_path, [subscribe])
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    monkeypatch.setattr(
        chain,
        "_process_search_subscription",
        lambda item, _searchchain, **_kwargs: item,
    )
    monkeypatch.setattr(
        chain,
        "_SubscribeChain__apply_subscribe_update",
        Mock(side_effect=RuntimeError("state cleanup failed")),
    )
    searchchain = Mock()

    def configure_site_budget(budget):
        """仅让释放站点预算的清理步骤失败。"""
        if budget is None:
            raise RuntimeError("budget cleanup failed")

    searchchain.configure_subscription_site_budget.side_effect = configure_site_budget

    with patch("app.chain.subscribe.search.SearchChain", return_value=searchchain):
        batch_id = chain.search(state="N")

    assert chain.get_search_batch(batch_id).state == "completed"
    replacement = chain._subscription_execution_admission.try_acquire(
        subscription_id=subscribe.id,
        operation="match",
        ttl_seconds=60,
    )
    assert replacement is not None
    assert chain._subscription_execution_admission.release(replacement) is True


def test_search_state_reset_stays_inside_subscription_admission(tmp_path, monkeypatch):
    """N 到 R 的本地状态写回完成前不能让 Match 接管同一订阅。"""
    subscribe = replace(_subscribe(8), state="N")
    chain = _chain(tmp_path, [subscribe])
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    monkeypatch.setattr(
        chain,
        "_process_search_subscription",
        lambda item, _searchchain, **_kwargs: item,
    )
    competing_leases = []

    def apply_update(*_args, **_kwargs):
        """在状态写回时探测同一订阅仍由 Search 持有。"""
        competing_leases.append(
            chain._subscription_execution_admission.try_acquire(
                subscription_id=subscribe.id,
                operation="match",
                ttl_seconds=60,
            )
        )

    monkeypatch.setattr(chain, "_SubscribeChain__apply_subscribe_update", apply_update)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        chain.search(state="N")

    assert competing_leases == [None]
    replacement = chain._subscription_execution_admission.try_acquire(
        subscription_id=subscribe.id,
        operation="match",
        ttl_seconds=60,
    )
    assert replacement is not None
    assert chain._subscription_execution_admission.release(replacement) is True


def test_late_cancel_completes_when_download_submission_already_started(tmp_path, monkeypatch):
    """取消晚于下载器副作用边界时按真实结果完成，不能伪装成未执行取消。"""
    subscribe = _subscribe(5)
    chain = _chain(tmp_path, [subscribe])
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")
    chain.subscription_download_repository = SimpleNamespace(
        has_started_for_task=lambda _task_id: False,
    )
    queue = chain.subscription_search_repository
    cancel_checks = iter((False, True))
    monkeypatch.setattr(queue, "is_cancel_requested", lambda _task_id: next(cancel_checks))
    def process(item, _searchchain, *, execution_context):
        """模拟当前任务复用或完成下载后才收到取消。"""
        execution_context.mark_download_started()
        return item

    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert batch.state == "completed"
    assert batch.finished_count == 1
    assert batch.cancelled_count == 0


def test_system_stop_requeues_task_after_search_returns(tmp_path, monkeypatch):
    """系统停机应阻止后续副作用，并把未完成任务退回可恢复队列。"""
    subscribe = _subscribe(10)
    chain = _chain(tmp_path, [subscribe])
    stop_state = SimpleNamespace(is_system_stopped=False)
    chain.stop_state = stop_state
    monkeypatch.setattr(chain, "_search_batch_available_at", lambda _source: "1970-01-01T00:00:00+00:00")

    def process(item, _searchchain, *, execution_context):
        """模拟站点搜索返回部分候选时进程进入停止阶段。"""
        stop_state.is_system_stopped = True
        assert execution_context.should_stop() is True
        return item

    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert batch.state == "queued"
    assert batch.finished_count == 0
    assert batch.failed_count == 0
    assert batch.cancelled_count == 0
    assert batch.skipped_count == 0

    stop_state.is_system_stopped = False
    recovered = chain.subscription_search_repository.claim_next(owner="worker-after-restart")
    assert recovered is not None
    assert recovered.subscription_id == subscribe.id
    assert recovered.attempt_count == 2
