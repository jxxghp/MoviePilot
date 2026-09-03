"""订阅搜索队列接入、锁隔离和批次失败治理测试。"""

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.site.observation import report_site_search_outcome
from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.execution import SubscriptionExecutionAdmission
from app.application.subscription.sitebudget import SubscriptionSearchCancelled
from app.chain.search.facade import SearchChain
from app.chain.subscribe import search as subscribe_search
from app.chain.subscribe.facade import SubscribeChain
from app.chain.subscribe.search import _search_task_available_at
from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository
from app.db.base import Base
from app.modules.indexer import IndexerModule
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


def _make_tasks_ready(monkeypatch) -> None:
    """让治理测试中的持久任务立即到期，避免依赖真实随机时钟。"""
    ready_at = "1970-01-01T00:00:00+00:00"
    monkeypatch.setattr(
        "app.chain.subscribe.search._search_task_available_at",
        lambda _source, subscription_ids: {
            subscription_id: ready_at for subscription_id in subscription_ids
        },
    )


def test_fallback_task_schedule_staggers_each_subscription(monkeypatch):
    """兜底批次首条抖动后，每条后续订阅都按独立随机间隔到期。"""
    now = datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc)
    delays = iter((12, 60, 300))
    monkeypatch.setattr(
        "app.chain.subscribe.search.random.randint",
        lambda _low, _high: next(delays),
    )

    schedule = _search_task_available_at(
        "fallback",
        (1, 2, 3),
        now=now,
    )

    available = [datetime.fromisoformat(schedule[subscribe_id]) for subscribe_id in (1, 2, 3)]
    assert (available[0] - now).total_seconds() == 12
    assert (available[1] - available[0]).total_seconds() == 60
    assert (available[2] - available[1]).total_seconds() == 300


def test_inline_fallback_search_preserves_site_pressure_stagger(tmp_path, monkeypatch):
    """无持久队列的兼容宿主仍须在每条自动兜底订阅前错峰。"""
    subscribes = [_subscribe(20), _subscribe(21)]
    chain = _chain(tmp_path, subscribes)
    del chain.subscription_search_repository
    waits = []
    delays = iter((60, 300))
    monkeypatch.setattr(
        "app.chain.subscribe.search.random.randint",
        lambda _low, _high: next(delays),
    )
    monkeypatch.setattr("app.chain.subscribe.search.time.sleep", waits.append)
    monkeypatch.setattr(
        chain,
        "_process_search_subscription",
        lambda item, _searchchain, **_kwargs: item,
    )
    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        chain.search(state="R")

    assert waits == [60, 300]


def test_successful_sites_remain_available_to_next_due_subscription(tmp_path, monkeypatch):
    """正常站点请求不能让同批下一条到期订阅漏掉目标站点。"""
    subscribes = [_subscribe(40), _subscribe(41)]
    chain = _chain(tmp_path, subscribes)
    _make_tasks_ready(monkeypatch)
    searchchain = object.__new__(SearchChain)
    searchchain.configure_subscription_site_budget(None)
    current_subscription_id = 0
    calls = []

    def search_site_torrents(*, site, **_kwargs):
        """记录真实发出的站点请求并发布正常完成观察结果。"""
        calls.append((current_subscription_id, site["id"]))
        report_site_search_outcome(attempted=True, outcome="success")
        return [f"torrent-{current_subscription_id}-{site['id']}"]

    def process(subscribe, current_searchchain, **_kwargs):
        """让每条到期订阅访问同一组完整目标站点。"""
        nonlocal current_subscription_id
        current_subscription_id = subscribe.id
        for site_id in (11, 12):
            current_searchchain._search_site_torrents_with_budget(  # pylint: disable=protected-access
                site={"id": site_id, "name": f"Site {site_id}"},
                keyword=subscribe.name,
                mtype=MediaType.MOVIE,
                page=0,
            )
        return subscribe

    searchchain.search_site_torrents = search_site_torrents
    monkeypatch.setattr(chain, "_process_search_subscription", process)
    info_logs = []
    monkeypatch.setattr(subscribe_search.logger, "info", info_logs.append)

    with patch("app.chain.subscribe.search.SearchChain", return_value=searchchain):
        batch_id = chain.search(state="R")

    assert calls == [(40, 11), (40, 12), (41, 11), (41, 12)]
    assert chain.get_search_batch(batch_id).state == "completed"
    assert "sites=2" in info_logs[-1]
    assert "site_requests=4" in info_logs[-1]
    assert "site_failures=0" in info_logs[-1]
    assert "site_cooldown_skips=0" in info_logs[-1]
    assert "candidates=4" in info_logs[-1]


def test_fallback_queue_executes_without_match_global_lock(tmp_path, monkeypatch):
    """R/P 兜底搜索在持久队列中执行，不受日常 Match 长锁阻塞。"""
    subscribes = [_subscribe(1), _subscribe(2)]
    chain = _chain(tmp_path, subscribes)
    _make_tasks_ready(monkeypatch)
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
    _make_tasks_ready(monkeypatch)
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


def test_search_logs_one_bounded_start_and_finish_summary(tmp_path, monkeypatch):
    """Search INFO 只保留轮次摘要，并携带任务终态与耗时字段。"""
    subscribes = [_subscribe(50), _subscribe(51)]
    chain = _chain(tmp_path, subscribes)
    _make_tasks_ready(monkeypatch)

    def process(subscribe, _searchchain, **_kwargs):
        """让一条任务失败并保持下一条正常收口。"""
        if subscribe.id == 50:
            raise RuntimeError("provider timeout")
        return subscribe

    monkeypatch.setattr(chain, "_process_search_subscription", process)
    info_logs = []
    monkeypatch.setattr(subscribe_search.logger, "info", info_logs.append)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    assert batch_id
    assert len(info_logs) == 2
    assert info_logs[0].startswith("订阅治理轮次开始: operation=search ")
    assert f"batch_id={batch_id}" in info_logs[0]
    assert "subscriptions=2" in info_logs[0]
    assert info_logs[1].startswith("订阅治理轮次结束: operation=search ")
    assert "state=failed" in info_logs[1]
    assert "processed=2" in info_logs[1]
    assert "task_completed=1" in info_logs[1]
    assert "task_failed=1" in info_logs[1]
    assert "admission_conflicts=0" in info_logs[1]
    assert "ttl_timeouts=0" in info_logs[1]
    assert "site_requests=0" in info_logs[1]
    assert "duration_ms=" in info_logs[1]
    assert "订阅成功" not in "\n".join(info_logs)


def test_search_logs_subscription_admission_release_failure(tmp_path, monkeypatch):
    """Search 无法释放订阅 owner 时必须即时告警并写入轮次摘要。"""
    subscribe = _subscribe(52)
    chain = _chain(tmp_path, [subscribe])
    _make_tasks_ready(monkeypatch)
    monkeypatch.setattr(
        chain,
        "_process_search_subscription",
        lambda item, _searchchain, **_kwargs: item,
    )
    monkeypatch.setattr(chain._subscription_execution_admission, "release", lambda _lease: False)
    info_logs = []
    error_logs = []
    monkeypatch.setattr(subscribe_search.logger, "info", info_logs.append)
    monkeypatch.setattr(
        subscribe_search.logger,
        "error",
        lambda message, **_kwargs: error_logs.append(message),
    )

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        chain.search(state="R")

    assert any("订阅准入释放失败: operation=search subscription_id=52" in item for item in error_logs)
    assert "release_failures=1" in info_logs[-1]


def test_queued_search_logs_failed_finish_summary_when_callback_raises(tmp_path, monkeypatch):
    """排队搜索开始后的外围异常仍须输出失败结束摘要。"""
    chain = _chain(tmp_path, [_subscribe(53)])
    _make_tasks_ready(monkeypatch)
    info_logs = []
    monkeypatch.setattr(subscribe_search.logger, "info", info_logs.append)

    with pytest.raises(RuntimeError, match="callback failed"):
        chain.search(
            state="R",
            progress_callback=Mock(side_effect=RuntimeError("callback failed")),
        )

    assert len(info_logs) == 2
    assert info_logs[-1].startswith("订阅治理轮次结束: operation=search ")
    assert "state=failed" in info_logs[-1]
    assert "round_failed=1" in info_logs[-1]


def test_resume_search_logs_failed_finish_summary_when_drain_raises(tmp_path, monkeypatch):
    """恢复消费开始后的外围异常仍须输出失败结束摘要。"""
    chain = _chain(tmp_path, [_subscribe(54)])
    info_logs = []
    monkeypatch.setattr(subscribe_search.logger, "info", info_logs.append)
    monkeypatch.setattr(
        chain,
        "_drain_search_queue",
        Mock(side_effect=RuntimeError("claim failed")),
    )

    with pytest.raises(RuntimeError, match="claim failed"):
        chain.resume_search_queue()

    assert len(info_logs) == 2
    assert info_logs[-1].startswith("订阅治理轮次结束: operation=search ")
    assert "source=resume" in info_logs[-1]
    assert "state=failed" in info_logs[-1]
    assert "round_failed=1" in info_logs[-1]


def test_swallowed_indexer_failure_marks_task_and_batch_failed_but_continues(tmp_path, monkeypatch):
    """索引器吞错后仍须失败收口当前任务，并继续处理后续订阅。"""
    subscribes = [_subscribe(13), _subscribe(14)]
    chain = _chain(tmp_path, subscribes)
    _make_tasks_ready(monkeypatch)
    searchchain = object.__new__(SearchChain)
    attempts = 0
    processed = []

    def execute_search(_site, _request):
        """首条站点请求模拟被 IndexerModule 吞掉的 HTTP 429。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("HTTP 429")
        return False, []

    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__search_check",
        staticmethod(lambda _site, _keyword=None: True),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__execute_search",
        staticmethod(execute_search),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__indexer_statistic",
        staticmethod(lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__parse_result",
        staticmethod(lambda **_kwargs: []),
    )
    searchchain.search_site_torrents = object.__new__(IndexerModule).search_torrents

    def process(subscribe, current_searchchain, **_kwargs):
        """让每条任务经过同一预算包装，并把观察到的站点失败抛给队列。"""
        processed.append(subscribe.id)
        current_searchchain._search_site_torrents_with_budget(  # pylint: disable=protected-access
            site={
                "id": 31 if subscribe.id == 13 else 32,
                "name": "Flaky" if subscribe.id == 13 else "Healthy",
            },
            keyword=subscribe.name,
            mtype=MediaType.MOVIE,
            page=0,
        )
        failures = current_searchchain.consume_subscription_site_budget_failures()
        if failures:
            raise RuntimeError("；".join(failures))
        return subscribe

    monkeypatch.setattr(chain, "_process_search_subscription", process)
    info_logs = []
    monkeypatch.setattr(subscribe_search.logger, "info", info_logs.append)

    with patch("app.chain.subscribe.search.SearchChain", return_value=searchchain):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert processed == [13, 14]
    assert batch.state == "failed"
    assert batch.finished_count == 1
    assert batch.failed_count == 1
    assert "Flaky" in batch.last_error
    assert "HTTP 429" in batch.last_error
    assert "sites=2" in info_logs[-1]
    assert "site_requests=2" in info_logs[-1]
    assert "site_failures=1" in info_logs[-1]
    assert "cooldown_seconds=900.0" in info_logs[-1]


def test_same_subscription_conflict_is_skipped_without_waiting(tmp_path, monkeypatch):
    """Match 已持有同一订阅时，Search 本轮完成为跳过且不进入业务处理。"""
    subscribe = _subscribe(6)
    chain = _chain(tmp_path, [subscribe])
    _make_tasks_ready(monkeypatch)
    process = Mock(side_effect=AssertionError("冲突订阅不应进入 Search"))
    monkeypatch.setattr(chain, "_process_search_subscription", process)
    match_lease = chain._subscription_execution_admission.try_acquire(
        subscription_id=subscribe.id,
        operation="match",
        ttl_seconds=60,
    )
    assert match_lease is not None
    info_logs = []
    monkeypatch.setattr(subscribe_search.logger, "info", info_logs.append)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    process.assert_not_called()
    assert batch.state == "skipped"
    assert batch.finished_count == 0
    assert batch.skipped_count == 1
    assert batch.last_error == "同一订阅正在由其他通道处理，本轮搜索已跳过"
    assert "task_skipped=1" in info_logs[-1]
    assert "admission_conflicts=1" in info_logs[-1]
    assert chain._subscription_execution_admission.release(match_lease) is True


def test_paused_subscription_is_skipped_after_admission_refresh(tmp_path, monkeypatch):
    """准入后重新读取到暂停订阅时不得开始搜索或伪造完成。"""
    subscribe = _subscribe(9)
    paused = replace(subscribe, state="S")
    chain = _chain(tmp_path, [subscribe])
    _make_tasks_ready(monkeypatch)
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
    _make_tasks_ready(monkeypatch)
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
    _make_tasks_ready(monkeypatch)
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


def test_late_cancel_completes_when_download_side_effect_already_started(tmp_path, monkeypatch):
    """取消晚于下载器副作用边界时按真实结果完成，不能伪装成未执行取消。"""
    subscribe = _subscribe(5)
    chain = _chain(tmp_path, [subscribe])
    _make_tasks_ready(monkeypatch)
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
    _make_tasks_ready(monkeypatch)

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


def test_system_stop_completes_when_download_side_effect_already_started(tmp_path, monkeypatch):
    """停机晚于下载器副作用边界时必须终结任务，避免重启后重复提交。"""
    subscribe = _subscribe(11)
    chain = _chain(tmp_path, [subscribe])
    stop_state = SimpleNamespace(is_system_stopped=False)
    chain.stop_state = stop_state
    _make_tasks_ready(monkeypatch)

    def process(item, _searchchain, *, execution_context):
        """模拟下载器已接收任务后进程进入停止阶段。"""
        execution_context.mark_download_started()
        stop_state.is_system_stopped = True
        return item

    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert batch.state == "completed"
    assert batch.finished_count == 1
    assert batch.failed_count == 0
    assert batch.cancelled_count == 0
    assert batch.skipped_count == 0
    assert chain.subscription_search_repository.claim_next(owner="worker-after-restart") is None


def test_ttl_expiry_after_normal_return_marks_task_and_batch_failed(tmp_path, monkeypatch):
    """正常返回也必须检查执行 TTL，未提交下载时按失败收口。"""
    subscribe = _subscribe(15)
    chain = _chain(tmp_path, [subscribe])
    _make_tasks_ready(monkeypatch)
    monkeypatch.setattr(
        chain._subscription_execution_admission,
        "is_expired",
        lambda _lease: True,
    )
    monkeypatch.setattr(
        chain,
        "_process_search_subscription",
        lambda item, _searchchain, **_kwargs: item,
    )
    info_logs = []
    monkeypatch.setattr(subscribe_search.logger, "info", info_logs.append)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert batch.state == "failed"
    assert batch.finished_count == 0
    assert batch.failed_count == 1
    assert batch.cancelled_count == 0
    assert batch.last_error == "订阅执行已超过协作截止时间"
    assert "task_failed=1" in info_logs[-1]
    assert "ttl_timeouts=1" in info_logs[-1]


def test_ttl_expiry_after_download_started_completes_with_actual_result(tmp_path, monkeypatch):
    """TTL 晚于下载器副作用边界时按实际结果完成，避免下轮重复提交。"""
    subscribe = _subscribe(16)
    chain = _chain(tmp_path, [subscribe])
    _make_tasks_ready(monkeypatch)
    monkeypatch.setattr(
        chain._subscription_execution_admission,
        "is_expired",
        lambda _lease: True,
    )

    def process(item, _searchchain, *, execution_context):
        """模拟下载器已接收任务后搜索链正常返回。"""
        execution_context.mark_download_started()
        return item

    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert batch.state == "completed"
    assert batch.finished_count == 1
    assert batch.failed_count == 0
    assert batch.cancelled_count == 0


def test_site_budget_ttl_expiry_marks_task_and_batch_failed(tmp_path, monkeypatch):
    """站点预算观察到执行 TTL 到期时必须失败收口，不能伪装成用户取消。"""
    subscribe = _subscribe(12)
    chain = _chain(tmp_path, [subscribe])
    _make_tasks_ready(monkeypatch)
    monkeypatch.setattr(
        chain._subscription_execution_admission,
        "is_expired",
        lambda _lease: True,
    )

    def process(_item, _searchchain, *, execution_context):
        """模拟站点预算将协作超时传播为搜索取消异常。"""
        assert execution_context.should_stop() is True
        raise SubscriptionSearchCancelled("订阅搜索已取消")

    monkeypatch.setattr(chain, "_process_search_subscription", process)

    with patch("app.chain.subscribe.search.SearchChain", return_value=Mock()):
        batch_id = chain.search(state="R")

    batch = chain.get_search_batch(batch_id)
    assert batch.state == "failed"
    assert batch.finished_count == 0
    assert batch.failed_count == 1
    assert batch.cancelled_count == 0
    assert batch.last_error == "订阅执行已超过协作截止时间"
